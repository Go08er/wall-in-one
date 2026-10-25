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
pub fn escaped_len(s: &str) -> usize {
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
    success_room(message).is_some()
}

/// How many bytes `Response::success(message)` leaves of `MAX_RESPONSE_BYTES`
/// on the wire, or None when it doesn't fit.
pub fn success_room(message: &str) -> Option<usize> {
    MAX_RESPONSE_BYTES.checked_sub(success_len(message))
}

/// How many bytes `Response::success(message)` takes past
/// `MAX_RESPONSE_BYTES` on the wire: 0 when it fits.
pub fn success_excess(message: &str) -> usize {
    success_len(message).saturating_sub(MAX_RESPONSE_BYTES)
}

/// `Response::success(message)`'s bytes on the wire, measured without
/// encoding it.
fn success_len(message: &str) -> usize {
    // `{"ok":true,"message":""}` and the newline.
    const ENVELOPE: usize = 25;
    ENVELOPE.saturating_add(escaped_len(message))
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
///
/// The deadline is looked at before every send, the first included: a
/// budget already spent (by encoding, or by the thread not being scheduled)
/// sends nothing, rather than as much as a writable socket would take.
pub fn write_response_until(
    stream: &UnixStream,
    response: &Response,
    deadline: Instant,
) -> Result<(), String> {
    let encoded = encode_response(response)?;
    let fd = stream.as_raw_fd();
    let mut unwritten = &encoded[..];
    while !unwritten.is_empty() {
        if deadline.saturating_duration_since(Instant::now()).is_zero() {
            return Err(format!(
                "response deadline expired with {} of {} bytes unwritten",
                unwritten.len(),
                encoded.len()
            ));
        }
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
        match error.raw_os_error() {
            // Interrupted before anything was sent: try again (deadline first).
            Some(libc::EINTR) if sent < 0 => continue,
            Some(libc::EAGAIN) if sent < 0 => {}
            _ => return Err(format!("cannot write response: {error}")),
        }
        // No room: wait for some, but not past the deadline.
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            continue; // reported at the top
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
    use std::io::{BufReader, ErrorKind, Read};
    use std::thread;
    use std::time::Duration;

    /// Everything the server has sent to `client` so far, without waiting.
    fn sent_so_far(client: &UnixStream) -> Vec<u8> {
        client.set_nonblocking(true).unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0_u8; 65536];
        loop {
            match (&*client).read(&mut chunk) {
                Ok(0) => break,
                Ok(read) => bytes.extend_from_slice(&chunk[..read]),
                Err(error) if error.kind() == ErrorKind::WouldBlock => break,
                Err(error) => panic!("{error}"),
            }
        }
        bytes
    }

    #[test]
    fn an_expired_deadline_sends_nothing() {
        // Codex 0.3.0 Y-5: the deadline was looked at only after a send found
        // no room, so a writable socket still got the whole reply.
        let (server, client) = UnixStream::pair().unwrap();
        let past = Instant::now().checked_sub(Duration::from_secs(1)).unwrap();
        let error = write_response_until(&server, &Response::success("small"), past).unwrap_err();
        assert!(error.contains("deadline expired"), "{error}");
        assert!(
            sent_so_far(&client).is_empty(),
            "nothing goes out once the budget is spent"
        );
    }

    #[test]
    fn a_budget_spent_before_the_first_send_sends_nothing() {
        let (server, client) = UnixStream::pair().unwrap();
        // Scheduling: the thread gets to the write only after its deadline.
        let deadline = Instant::now() + Duration::from_millis(20);
        thread::sleep(Duration::from_millis(40));
        let error =
            write_response_until(&server, &Response::success("small"), deadline).unwrap_err();
        assert!(error.contains("deadline expired"), "{error}");
        assert!(sent_so_far(&client).is_empty());
        // Encoding: a near-full reply takes far longer than a microsecond to
        // encode, so its budget is gone before the first send.
        let large = Response::success("x".repeat(MAX_RESPONSE_BYTES - 64));
        let deadline = Instant::now() + Duration::from_micros(1);
        let error = write_response_until(&server, &large, deadline).unwrap_err();
        assert!(error.contains("deadline expired"), "{error}");
        assert!(sent_so_far(&client).is_empty());
    }

    #[test]
    fn a_reading_peer_gets_the_whole_reply() {
        let (server, client) = UnixStream::pair().unwrap();
        let response = Response::success("y".repeat(512 * 1024));
        let expected = encode_response(&response).unwrap();
        let reader = thread::spawn(move || {
            let mut all = Vec::new();
            (&client).read_to_end(&mut all).unwrap();
            all
        });
        write_response_until(&server, &response, Instant::now() + Duration::from_secs(5)).unwrap();
        drop(server);
        assert_eq!(reader.join().unwrap(), expected);
    }

    #[test]
    fn a_stalled_peer_is_given_up_at_the_deadline() {
        let (server, _client) = UnixStream::pair().unwrap();
        let started = Instant::now();
        let deadline = started + Duration::from_millis(150);
        let response = Response::success("z".repeat(900 * 1024));
        let error = write_response_until(&server, &response, deadline).unwrap_err();
        let elapsed = started.elapsed();
        assert!(error.contains("deadline expired"), "{error}");
        assert!(
            elapsed >= Duration::from_millis(150) && elapsed < Duration::from_secs(1),
            "{elapsed:?}"
        );
    }

    /// `client`'s socket closed for good: shut down, then dropped. Dropping alone closes only
    /// this process's descriptor, and another test thread's spawn can hold a copy of it from
    /// its fork to its exec, the peer then still open to a send (review r13g: beside a thread
    /// spawning `/bin/true`, 483 of 20,000 such sends succeeded). A shutdown closes the socket
    /// itself, whoever holds a descriptor of it. The service's own peer is another process.
    fn closed_for_good(client: UnixStream) {
        client.shutdown(std::net::Shutdown::Both).unwrap();
        drop(client);
    }

    #[test]
    fn a_closed_peer_fails_at_once() {
        let (server, client) = UnixStream::pair().unwrap();
        closed_for_good(client);
        let started = Instant::now();
        let deadline = started + Duration::from_secs(5);
        let error = write_response_until(&server, &Response::success("w"), deadline).unwrap_err();
        assert!(error.contains("cannot write response"), "{error}");
        assert!(started.elapsed() < Duration::from_millis(500));
    }

    #[test]
    fn a_closed_peer_fails_at_once_beside_spawns() {
        // The case above, many times, beside a thread that keeps spawning processes, as the
        // renderer tests in this binary do: every send fails, at once.
        use std::sync::Arc;
        use std::sync::atomic::{AtomicBool, Ordering};
        let stop = Arc::new(AtomicBool::new(false));
        let spawner = {
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    let _ = std::process::Command::new("/bin/true").status();
                }
            })
        };
        let sent: usize = (0..5_000)
            .map(|_| {
                let (server, client) = UnixStream::pair().unwrap();
                closed_for_good(client);
                let deadline = Instant::now() + Duration::from_secs(5);
                usize::from(
                    write_response_until(&server, &Response::success("w"), deadline).is_ok(),
                )
            })
            .sum();
        stop.store(true, Ordering::Relaxed);
        spawner.join().unwrap();
        assert_eq!(sent, 0, "sends to a closed peer that succeeded");
    }

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
        assert_eq!(success_room(&"x".repeat(limit)), Some(0));
        assert_eq!(success_room(&"\"".repeat(10)), Some(limit - 20));
        assert_eq!(success_room(&"x".repeat(limit + 1)), None);
        assert_eq!(success_excess(&"x".repeat(limit)), 0);
        assert_eq!(success_excess(&"x".repeat(limit + 7)), 7);
        assert_eq!(
            success_excess(&"\"".repeat(limit / 2 + 1)),
            2 * (limit / 2 + 1) - limit
        );
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
