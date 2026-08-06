// A synthetic target with a knob for every bottleneck Performer hunts.
//
// The tool has to be shown to reach the *right* conclusion, not merely to
// produce output, and that needs a workload whose truth is known in advance.
// This program provides one:
//
//   --threads N        how many worker threads (the 315 thread case)
//   --contention P     percent of iterations that take the one shared mutex
//   --hold-us U        microseconds spent inside that critical section
//   --sleep-us U       microseconds slept per iteration (timer/idle pressure)
//   --work N           units of pure user-space CPU per iteration
//   --seconds S        run time, 0 = until SIGTERM
//
// With --contention 90 --hold-us 50 the dominant cost is one mutex, and a
// correct analysis must name it. With --contention 0 --sleep-us 1000 the same
// threads are almost entirely off-CPU and no lock should be blamed.
//
// Build:  make -C tests/target
// The Makefile passes -fno-omit-frame-pointer on purpose: this is also the
// program used to check that the frame pointer preflight passes when it
// should.

#include <pthread.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Options {
  int threads = 8;
  int contention_pct = 50;
  int hold_us = 20;
  int sleep_us = 200;
  int work_units = 200;
  int seconds = 10;
};

std::mutex g_shared_mutex;      // the one hot lock
std::atomic<long> g_shared_counter{0};
std::atomic<bool> g_running{true};
std::atomic<long> g_iterations{0};

// Deliberately not inlined so the frames show up in a stack trace with a
// meaningful name. Each is a distinct call path the profiler should separate.
__attribute__((noinline)) long burn_cpu(int units) {
  long acc = 0;
  for (int i = 0; i < units * 64; ++i) {
    acc += (i * 2654435761u) % 1000;
  }
  return acc;
}

__attribute__((noinline)) void hold_shared_lock(int hold_us) {
  std::lock_guard<std::mutex> guard(g_shared_mutex);
  const auto until = std::chrono::steady_clock::now() +
                     std::chrono::microseconds(hold_us);
  while (std::chrono::steady_clock::now() < until) {
    g_shared_counter.fetch_add(1, std::memory_order_relaxed);
  }
}

__attribute__((noinline)) void sleep_a_while(int sleep_us) {
  std::this_thread::sleep_for(std::chrono::microseconds(sleep_us));
}

__attribute__((noinline)) void worker_loop(const Options& opt, int index) {
  unsigned seed = static_cast<unsigned>(index * 2654435761u + 12345u);
  volatile long sink = 0;
  while (g_running.load(std::memory_order_relaxed)) {
    seed = seed * 1103515245u + 12345u;
    const int roll = static_cast<int>((seed >> 16) % 100);
    if (roll < opt.contention_pct) {
      hold_shared_lock(opt.hold_us);
    } else {
      sink += burn_cpu(opt.work_units);
    }
    if (opt.sleep_us > 0) {
      sleep_a_while(opt.sleep_us);
    }
    g_iterations.fetch_add(1, std::memory_order_relaxed);
  }
  (void)sink;
}

int int_arg(int argc, char** argv, int i, const char* name) {
  if (i + 1 >= argc) {
    std::fprintf(stderr, "%s needs a value\n", name);
    std::exit(2);
  }
  return std::atoi(argv[i + 1]);
}

}  // namespace

int main(int argc, char** argv) {
  Options opt;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--threads") { opt.threads = int_arg(argc, argv, i++, "--threads"); }
    else if (arg == "--contention") { opt.contention_pct = int_arg(argc, argv, i++, "--contention"); }
    else if (arg == "--hold-us") { opt.hold_us = int_arg(argc, argv, i++, "--hold-us"); }
    else if (arg == "--sleep-us") { opt.sleep_us = int_arg(argc, argv, i++, "--sleep-us"); }
    else if (arg == "--work") { opt.work_units = int_arg(argc, argv, i++, "--work"); }
    else if (arg == "--seconds") { opt.seconds = int_arg(argc, argv, i++, "--seconds"); }
    else if (arg == "--help") {
      std::printf(
          "usage: contention [--threads N] [--contention PCT] [--hold-us U]\n"
          "                  [--sleep-us U] [--work N] [--seconds S]\n");
      return 0;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argv[i]);
      return 2;
    }
  }
  if (opt.threads < 1) opt.threads = 1;

  // Printed so a test harness can wait for readiness instead of sleeping and
  // hoping the threads exist yet.
  std::printf("pid %d threads %d contention %d%% hold %dus sleep %dus\n",
              static_cast<int>(getpid()), opt.threads, opt.contention_pct,
              opt.hold_us, opt.sleep_us);
  std::fflush(stdout);

  std::vector<std::thread> workers;
  workers.reserve(opt.threads);
  for (int i = 0; i < opt.threads; ++i) {
    workers.emplace_back(worker_loop, std::cref(opt), i);
  }

  // Name the threads so the folded stacks and the Threads table are readable.
  // Linux caps thread names at 15 characters plus NUL, so the name is built
  // wide and then deliberately truncated to fit.
  for (int i = 0; i < opt.threads; ++i) {
    char wide[32];
    char name[16];
    std::snprintf(wide, sizeof(wide), "worker%d", i);
    std::strncpy(name, wide, sizeof(name) - 1);
    name[sizeof(name) - 1] = '\0';
    pthread_setname_np(workers[i].native_handle(), name);
  }

  std::printf("ready\n");
  std::fflush(stdout);

  if (opt.seconds > 0) {
    std::this_thread::sleep_for(std::chrono::seconds(opt.seconds));
    g_running.store(false, std::memory_order_relaxed);
  } else {
    while (g_running.load(std::memory_order_relaxed)) {
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }

  for (auto& worker : workers) {
    worker.join();
  }
  std::printf("iterations %ld\n", g_iterations.load());
  return 0;
}
