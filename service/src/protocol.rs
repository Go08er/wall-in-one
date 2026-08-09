use serde::{Deserialize, Serialize};
use std::io::{BufRead, Write};

pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;

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
    let mut bytes = Vec::new();
    loop {
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
            return Err("request exceeds 65536 bytes".into());
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

pub fn write_response(writer: &mut impl Write, response: &Response) -> Result<(), String> {
    let mut encoded = serde_json::to_vec(response).map_err(|error| error.to_string())?;
    encoded.push(b'\n');
    if encoded.len() > MAX_MESSAGE_BYTES {
        return Err("response exceeds 65536 bytes".into());
    }
    writer
        .write_all(&encoded)
        .map_err(|error| format!("cannot write response: {error}"))
}
