#pragma once

#include <dwrite.h>
#include <wrl/client.h>

#include <string>
#include <vector>

namespace krok::subtitle::native::direct2d {

// Resolve one family name to a font face.  ``collection`` is the classic
// GDI-model system collection; ``typographicCollection`` (nullable) is the
// typographic-model collection that groups variable fonts under merged
// family names.  The name is tried typographic-first, then classic, then
// the Win32 informational-name scan, so both spellings the Qt font picker
// offers resolve to the face the CPU renderer draws.
Microsoft::WRL::ComPtr<IDWriteFontFace> createFontFace(
    IDWriteFontCollection *collection,
    IDWriteFontCollection *typographicCollection,
    const std::wstring &familyName,
    int weight,
    bool italic
);

bool containsEmoji(const std::wstring &text);

std::vector<UINT16> glyphIndices(
    IDWriteFontFace *face,
    const std::wstring &text
);

bool validGlyphIndices(const std::vector<UINT16> &glyphs);

Microsoft::WRL::ComPtr<IDWriteFontFace> findFallbackFontFace(
    IDWriteFontCollection *collection,
    const std::wstring &text,
    std::vector<Microsoft::WRL::ComPtr<IDWriteFontFace>> &successfulFaces,
    std::vector<UINT16> &glyphs
);

}  // namespace krok::subtitle::native::direct2d
