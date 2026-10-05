use serde::{Deserialize, Serialize};
use std::io::{BufRead, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::time::Instant;

pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;
// A status snapshot can legitimately contain the configured maximum of 512
// playlists and 512 schedule rules. Replies get a larger independent ceiling;
// requests remain at the deliberately small line-protocol bound above.
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct Request {
    pub verb: String,
    #[serde(default)]
    pub argument: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Response {
    pub ok: bool,
    pub message: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub kind: String,
}

impl Response {
    pub fn success(message: impl Into<String>) -> Self {
        Self {
            ok: true,
            message: message.into(),
            kind: String::new(),
        }
    }

    pub fn failure(message: impl Into<String>) -> Self {
        Self {
            ok: false,
            message: message.into(),
            kind: String::new(),
        }
    }
}

pub fn read_request(reader: &mut impl BufRead) -> Result<Request, String> {
    read_request_budgeted(reader, || Ok(()))
}

pub fn read_request_until(
    reader: &mut impl BufRead,
    stream: &UnixStream,
    deadline: Instant,
) -> Result<Request, String> {
    read_request_budgeted(reader, || {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("request deadline expired".into());
        }
        stream
            .set_read_timeout(Some(remaining))
            .map_err(|error| format!("cannot configure request deadline: {error}"))
    })
}

fn read_request_budgeted(
    reader: &mut impl BufRead,
    mut before_read: impl FnMut() -> Result<(), String>,
) -> Result<Request, String> {
    let mut bytes = Vec::new();
    loop {
        before_read()?;
        let available = reader
            .fill_buf()
            .map_err(|error| format!("cannot read request: {error}"))?;
        if available.is_empty() {
            break;
        }
        let count = available
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(available.len(), |index| index + 1);
        if bytes.len() + count > MAX_MESSAGE_BYTES {
            return Err(format!("request exceeds {MAX_MESSAGE_BYTES} bytes"));
        }
        bytes.extend_from_slice(&available[..count]);
        reader.consume(count);
        if bytes.last() == Some(&b'\n') {
            break;
        }
    }
    if bytes.is_empty() {
        return Err("empty request".into());
    }
    serde_json::from_slice(&bytes).map_err(|error| format!("invalid request: {error}"))
}

/// The bytes `s` takes as a JSON string's contents, escaped as serde_json
/// escapes it: `"` and `\` take two, the control characters with a short
/// escape two and the others six (`\u00XX`); everything else is itself.
fn escaped_len(s: &str) -> usize {
    s.bytes()
        .map(|byte| match byte {
            b'"' | b'\\' | b'\n' | b'\r' | b'\t' | 0x08 | 0x0c => 2,
            0x00..=0x1f => 6,
            _ => 1,
        })
        .sum()
}

/// Whether `Response::success(message)` fits `MAX_RESPONSE_BYTES` on the
/// wire, measured without encoding it.
pub fn success_fits(message: &str) -> bool {
    // `{"ok":true,"message":""}` and the newline.
    const ENVELOPE: usize = 25;
    ENVELOPE.saturating_add(escaped_len(message)) <= MAX_RESPONSE_BYTES
}

fn encode_response(response: &Response) -> Result<Vec<u8>, String> {
    let mut encoded = serde_json::to_vec(response).map_err(|error| error.to_string())?;
    encoded.push(b'\n');
    if encoded.len() > MAX_RESPONSE_BYTES {
        return Err(format!("response exceeds {MAX_RESPONSE_BYTES} bytes"));
    }
    Ok(encoded)
}

pub fn write_response(writer: &mut impl Write, response: &Response) -> Result<(), String> {
    let encoded = encode_response(response)?;
    writer
        .write_all(&encoded)
        .map_err(|error| format!("cannot write response: {error}"))
}

/// Write `response` to a client within `deadline` as a whole.
///
/// A socket write timeout bounds each wait for buffer space, not the write:
/// a peer that reads just often enough keeps one blocking `write` going for
/// as long as the reply takes to trickle out, and with it the single service
/// thread. So the reply goes out in nonblocking pieces, waiting for room with
/// `poll` only until `deadline`; at it the reply is abandoned and the runtime
/// loop goes back to its work.
pub fn write_response_until(
    stream: &UnixStream,
    response: &Response,
    deadline: Instant,
) -> Result<(), String> {
    let encoded = encode_response(response)?;
    let fd = stream.as_raw_fd();
    let mut unwritten = &encoded[..];
    while !unwritten.is_empty() {
        // SAFETY: the pointer and length describe the live `unwritten` slice.
        let sent = unsafe {
            libc::send(
                fd,
                unwritten.as_ptr().cast(),
                unwritten.len(),
                libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL,
            )
        };
        if sent > 0 {
            unwritten = &unwritten[sent.unsigned_abs()..];
            continue;
        }
        let error = std::io::Error::last_os_error();
        if sent == 0 || !matches!(error.raw_os_error(), Some(libc::EAGAIN | libc::EINTR)) {
            return Err(format!("cannot write response: {error}"));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(format!(
                "response deadline expired with {} of {} bytes unwritten",
                unwritten.len(),
                encoded.len()
            ));
        }
        let mut ready = libc::pollfd {
            fd,
            events: libc::POLLOUT,
            revents: 0,
        };
        let wait = i32::try_from(remaining.as_millis())
            .unwrap_or(i32::MAX)
            .max(1);
        // SAFETY: one valid pollfd for the duration of the call.
        if unsafe { libc::poll(&mut ready, 1, wait) } < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EINTR) {
                return Err(format!("cannot wait to write response: {error}"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufReader;
    use std::thread;
    use std::time::Duration;

    #[test]
    fn the_fit_measure_is_the_encoded_size() {
        for message in [
            String::new(),
            "plain text".into(),
            "\"quoted\" and \\back\\slashed".into(),
            "line\nbreak\r\ttab\u{8}\u{c}".into(),
            "\u{0}\u{1}\u{1f}\u{7f}".into(),
            "gr\u{fc}\u{df}e \u{1f600} \u{2028}".into(),
            "\"".repeat(1000),
        ] {
            let encoded = serde_json::to_vec(&Response::success(message.clone())).unwrap();
            assert_eq!(25 + escaped_len(&message), encoded.len() + 1, "{message:?}");
        }
        let limit = MAX_RESPONSE_BYTES - 25;
        assert!(success_fits(&"x".repeat(limit)));
        assert!(!success_fits(&"x".repeat(limit + 1)));
        assert!(!success_fits(&"\"".repeat(limit / 2 + 1)));
    }

    #[test]
    fn trickled_request_cannot_renew_the_absolute_deadline() {
        let (receiver, mut sender) = UnixStream::pair().unwrap();
        let worker = thread::spawn(move || {
            for _ in 0..12 {
                if sender.write_all(b" ").is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(30));
            }
        });
        let started = Instant::now();
        let error = read_request_until(
            &mut BufReader::new(&receiver),
            &receiver,
            started + Duration::from_millis(100),
        )
        .unwrap_err();
        assert!(started.elapsed() < Duration::from_millis(300), "{error}");
        drop(receiver);
        worker.join().unwrap();
    }
}
