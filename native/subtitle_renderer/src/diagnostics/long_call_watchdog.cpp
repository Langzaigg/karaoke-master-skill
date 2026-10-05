#include "long_call_watchdog.h"

#include "../protocol/json_protocol.h"

#include <QtCore/QString>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace krok::subtitle::native::diagnostics {
namespace {

struct DeclaredCall {
    unsigned long long id = 0;
    long long startedMs = 0;
    long long deadlineMs = 0;
    std::string phase;
};

std::mutex g_mutex;
std::vector<DeclaredCall> g_active;
unsigned long long g_nextId = 1;
std::thread g_feeder;
bool g_feederRunning = false;

long long nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()
    ).count();
}

// 预算倍率（诊断用）：KROK_GPU_LONG_CALL_BUDGET_SCALE，默认 1.0。
double budgetScale() {
    const char *raw = std::getenv("KROK_GPU_LONG_CALL_BUDGET_SCALE");
    if (raw == nullptr || *raw == '\0') {
        return 1.0;
    }
    try {
        const double value = std::atof(raw);
        return value > 0.0 ? value : 1.0;
    } catch (...) {
        return 1.0;
    }
}

void feederLoop() {
    while (true) {
        {
            std::lock_guard<std::mutex> lock(g_mutex);
            if (!g_feederRunning) {
                return;
            }
        }
        const long long current = nowMs();
        std::string phase;
        long long startedMs = 0;
        {
            std::lock_guard<std::mutex> lock(g_mutex);
            // 预算内 newest 的声明优先上报；没有任何预算内的声明就不发
            // （调用卡死超预算 / 无长调用 / 线程空闲都属于不该喂的状态）。
            for (const DeclaredCall &call : g_active) {
                if (call.deadlineMs > current) {
                    if (phase.empty() || call.startedMs > startedMs) {
                        phase = call.phase;
                        startedMs = call.startedMs;
                    }
                }
            }
        }
        if (!phase.empty()) {
            protocol::emitProgress(
                QString::fromStdString(phase), 0, 0
            );
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    }
}

}  // namespace

LongCallScope::LongCallScope(const char *phase, double budgetSeconds)
    : id_(0) {
    if (phase == nullptr || budgetSeconds <= 0.0) {
        return;
    }
    DeclaredCall call;
    call.startedMs = nowMs();
    const long long budgetMs = static_cast<long long>(
        budgetSeconds * 1000.0 * budgetScale()
    );
    call.deadlineMs = call.startedMs + std::max<long long>(budgetMs, 1000);
    call.phase = phase;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        call.id = g_nextId++;
        g_active.push_back(call);
        id_ = call.id;
    }
}

LongCallScope::~LongCallScope() {
    if (id_ == 0) {
        return;
    }
    std::lock_guard<std::mutex> lock(g_mutex);
    g_active.erase(
        std::remove_if(
            g_active.begin(), g_active.end(),
            [this](const DeclaredCall &call) { return call.id == id_; }
        ),
        g_active.end()
    );
}

void startLongCallFeeder() {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_feederRunning) {
        return;
    }
    g_feederRunning = true;
    g_feeder = std::thread(feederLoop);
}

void stopLongCallFeeder() {
    std::thread feeder;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        if (!g_feederRunning) {
            return;
        }
        g_feederRunning = false;
        feeder = std::move(g_feeder);
    }
    if (feeder.joinable()) {
        feeder.join();
    }
}

}  // namespace krok::subtitle::native::diagnostics
