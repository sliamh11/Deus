use std::io;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::platform;

pub struct ClipboardImage {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct Attachment {
    pub path: PathBuf,
    pub width: u32,
    pub height: u32,
}

fn cache_dir() -> PathBuf {
    dirs::cache_dir()
        .unwrap_or_else(|| platform::home_dir().join(".cache"))
        .join("deus")
        .join(format!("clipboard-images-{}", std::process::id()))
}

pub fn probe_image() -> Option<ClipboardImage> {
    let mut clipboard = arboard::Clipboard::new().ok()?;
    let img = clipboard.get_image().ok()?;
    Some(ClipboardImage {
        width: img.width as u32,
        height: img.height as u32,
        rgba: img.bytes.into_owned(),
    })
}

pub fn save_image(img: &ClipboardImage) -> io::Result<Attachment> {
    let dir = cache_dir();
    std::fs::create_dir_all(&dir)?;

    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let path = dir.join(format!("clip-{}.png", ts));

    let rgba_image = image::RgbaImage::from_raw(img.width, img.height, img.rgba.clone())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid RGBA dimensions"))?;
    rgba_image.save(&path).map_err(io::Error::other)?;

    Ok(Attachment {
        path,
        width: img.width,
        height: img.height,
    })
}

// PNGs are kept alive during the session so the agent can Read them at any point.
pub fn cleanup() {
    let _ = std::fs::remove_dir_all(cache_dir());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_and_cleanup_roundtrip() {
        let img = ClipboardImage {
            width: 2,
            height: 2,
            rgba: vec![
                255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 255, 255,
            ],
        };
        let att = save_image(&img).unwrap();
        assert!(att.path.exists());
        assert_eq!(att.width, 2);
        assert_eq!(att.height, 2);

        cleanup();
        assert!(!cache_dir().exists());
    }

    #[test]
    fn invalid_dimensions_errors() {
        let img = ClipboardImage {
            width: 100,
            height: 100,
            rgba: vec![0; 4],
        };
        assert!(save_image(&img).is_err());
    }
}
