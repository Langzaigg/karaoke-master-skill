#pragma once

#include <string>
#include <utility>

namespace krok::subtitle::native {

bool isLatinText(const std::wstring &text);
// Ruby auto-alignment prefers-center test, mirroring the CPU painter's
// _ruby_auto_prefers_center (identical character ranges): center when Latin
// alnum characters OUTNUMBER the other visible characters (whitespace
// excluded, ties fall back to equal-space), or when every visible character
// is a symbol (no Latin alnum and no script letter at all).  N3's pure-alnum
// check made readings like "e-bay" fall into equal-space and tear the
// letters apart.
bool rubyAutoCenterLayout(const std::wstring &text);
bool isWhitespaceText(const std::wstring &text);
bool verticalRotates(const std::wstring &text);

std::pair<float, float> verticalGlyphOffset(
    const std::wstring &text,
    float cellWidth,
    float cellHeight
);

}  // namespace krok::subtitle::native
