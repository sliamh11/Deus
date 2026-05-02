use unicode_bidi::{BidiInfo, Level};

pub fn visual_reorder(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    if !text.chars().any(|c| is_rtl_char(c)) {
        return text.to_string();
    }
    // Force LTR base: Ratatui renders LTR only, so we reorder RTL runs into visual LTR order
    let bidi = BidiInfo::new(text, Some(Level::ltr()));
    let para = &bidi.paragraphs[0];
    let line = para.range.clone();
    let reordered = bidi.reorder_line(para, line);
    reordered.to_string()
}

fn is_rtl_char(c: char) -> bool {
    let cp = c as u32;
    (0x0590..=0x05FF).contains(&cp)   // Hebrew
    || (0x0600..=0x06FF).contains(&cp) // Arabic
    || (0xFB50..=0xFDFF).contains(&cp) // Arabic Presentation A
    || (0xFE70..=0xFEFF).contains(&cp) // Arabic Presentation B
    || (0xFB1D..=0xFB4F).contains(&cp) // Hebrew Presentation
}
