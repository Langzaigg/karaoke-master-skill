#include "native_trace.h"

#include <QByteArray>

#include <cstdarg>
#include <cstdio>

namespace krok::subtitle::native::diagnostics {

namespace {

bool traceEnabled() noexcept
{
    static const bool enabled = qEnvironmentVariableIsSet("KROK_NATIVE_TRACE")
        && qgetenv("KROK_NATIVE_TRACE").trimmed() == QByteArray("1");
    return enabled;
}

}  // namespace

void nativeTrace(const char *format, ...) noexcept
{
    if (!traceEnabled()) {
        return;
    }
    std::fputs("[native-trace] ", stderr);
    va_list args;
    va_start(args, format);
    std::vfprintf(stderr, format, args);
    va_end(args);
    std::fputc('\n', stderr);
    std::fflush(stderr);
}

}  // namespace krok::subtitle::native::diagnostics
