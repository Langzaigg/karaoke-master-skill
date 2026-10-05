#pragma once

namespace krok::subtitle::native::diagnostics {

// 预算制长调用代喂（2026-10 看门狗补盲，RAII）。
//
// 单体 D2D 调用（Widen / CreateStrokedGeometryRealization 等）一旦进入
// 不可中断，调用线程在返回前发不出任何心跳——密集矢量路径上单次可达
// 数十秒，主线程被它阻塞期间空闲定时器也发不出来，逐段心跳的循环也被
// 卡在单个迭代里。这是纯超时与逐段喂狗共同的盲区。
//
// 本 RAII 在进入此类调用前登记「阶段名 + 时长预算」，全局喂狗线程在
// 预算内每秒代发一拍心跳（调用线程即使阻塞，进程仍有存活证据）；
// 超出预算即停喂——若调用真卡死，GUI 租期到期照常判死。检测延迟 =
// 预算 + 租期，有界；预算必须按已知最坏合法耗时放宽（宁大勿小）。
class LongCallScope {
public:
    // budgetSeconds <= 0 表示不登记（该调用退化为不代喂）。
    explicit LongCallScope(const char *phase, double budgetSeconds);
    ~LongCallScope();
    LongCallScope(const LongCallScope &) = delete;
    LongCallScope &operator=(const LongCallScope &) = delete;

private:
    unsigned long long id_ = 0;
};

// 喂狗线程生命周期（main.cpp 调用；幂等）。线程平时 1s 轮询登记表，
// 有预算内的活跃声明才发心跳，空闲开销可忽略。
void startLongCallFeeder();
void stopLongCallFeeder();

}  // namespace krok::subtitle::native::diagnostics
