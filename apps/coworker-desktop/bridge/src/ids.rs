use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};

pub fn new_compact_id(prefix: &str) -> String {
    let value = uuid::Uuid::new_v4();
    format!("{prefix}{}", URL_SAFE_NO_PAD.encode(&value.as_bytes()[..9]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compact_id_is_model_friendly() {
        let request_id = new_compact_id("req_");
        assert_eq!(request_id.len(), 16);
        assert!(request_id.starts_with("req_"));
        assert!(
            request_id[4..]
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_')
        );
    }
}
