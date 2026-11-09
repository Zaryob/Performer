// A deterministic region for the real GDB integration test.
#include <chrono>
#include <thread>

extern "C" __attribute__((noinline)) void region_start() {
    asm volatile("" ::: "memory");
}
extern "C" __attribute__((noinline)) void region_end() {
    asm volatile("" ::: "memory");
}
int main() {
    region_start();
    std::thread worker([] { std::this_thread::sleep_for(std::chrono::milliseconds(100)); });
    worker.join();
    region_end();
    return 0;
}
