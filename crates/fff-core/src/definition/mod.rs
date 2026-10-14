mod languages;
mod parser;
mod rules;
mod suffix;

pub(crate) use languages::{Language, is_definition_match};

#[cfg(test)]
mod tests {
    use super::{Language, is_definition_match};

    #[test]
    fn generated_rules_classify_declared_names() {
        for (filename, line, expected) in [
            ("names.rs", "#[derive(Clone)] pub struct user_file;", true),
            ("names.rs", "impl user_file {}", false),
            ("names.py", "class user_file(Base):", true),
            ("names.py", "def other(value: user_file):", false),
            ("names.go", "type user_file struct {}", true),
            ("names.go", "func (s *user_file) other() {}", false),
            ("names.ts", "export class user_file extends Base {}", true),
            ("names.ts", "export const enum user_file { Ready }", true),
            ("names.ts", "const current: user_file = Mode.Ready;", false),
            ("names.ts", "function other(value: user_file) {}", false),
            ("names.lua", "local function user_file() end", true),
            ("names.lua", "function user_file.other() end", false),
            ("names.cpp", "struct user_file {", true),
            ("names.cpp", "struct Packet *user_file(void) {", true),
            ("names.cpp", "struct user_file *create(void) {", false),
            ("names.cpp", "struct user_file instance;", false),
            ("names.java", "public class user_file extends Base {", true),
            (
                "names.java",
                "public static <T> List<T> user_file(T... xs) {",
                true,
            ),
            (
                "names.java",
                "public static <user_file> List<user_file> create() {",
                false,
            ),
            ("names.java", "public user_file other() {", false),
            ("names.kt", "data class user_file(val x: Int)", true),
            ("names.kt", "fun user_file.other() {}", false),
            ("names.scala", "case class user_file(x: Int)", true),
            ("names.scala", "extension (value: user_file)", false),
            ("names.swift", "public struct user_file {", true),
            ("names.swift", "extension user_file {", false),
            ("names.rb", "def self.user_file?", true),
            ("names.rb", "def user_file.other", false),
            ("names.ex", "defp user_file?(arg) do", true),
            ("names.ex", "defimpl user_file, for: Other do", false),
            ("names.zig", "pub const user_file = struct {", true),
            ("names.zig", "fn other(value: user_file) void {", false),
            ("names.php", "#[Attribute] class user_file {", true),
            ("names.php", "class Other extends user_file {", false),
            ("names.sh", "user_file() {", true),
            ("names.sh", "echo user_file", false),
            ("names.ml", "type 'a user_file = 'a list", true),
            ("names.ml", "let other = user_file value", false),
            ("names.hs", "user_file :: Int -> Int", true),
        ] {
            let start = line.find("user_file").unwrap() as u32;
            let ranges = [(start, start + 9)];
            let language = Language::from_filename(filename) as u8;
            for fuzzy in [false, true] {
                assert_eq!(
                    is_definition_match(language, line.as_bytes(), &ranges, fuzzy),
                    expected,
                    "{filename}: {line}"
                );
            }
        }
    }

    #[test]
    fn fuzzy_definition_ranges_stay_inside_the_name() {
        let language = Language::from_filename("file.rs") as u8;
        let line = b"fn user(file: u8) {}";
        assert!(is_definition_match(language, line, &[(3, 7)], true));
        assert!(!is_definition_match(
            language,
            line,
            &[(3, 7), (8, 12)],
            true
        ));
        assert!(!is_definition_match(language, line, &[], true));
        assert!(!is_definition_match(language, line, &[(3, 3)], true));
        assert!(!is_definition_match(255, line, &[(3, 7)], true));
    }
}
