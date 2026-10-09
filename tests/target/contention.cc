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
//   --churn-ms N       create one short-lived thread every N milliseconds
//   --io-kb N          write N KiB to a per-thread file and fdatasync it
//                      every iteration (I/O pressure; 0 disables)
//   --io-dir PATH      directory for those files (default /tmp); they are
//                      unlinked on creation, so nothing is left behind
//   --report-ms N      print "tick <clock_ns> <iterations> <cpu_ns>" every N
//                      milliseconds so a benchmark can measure throughput
//                      inside an exact window (CLOCK_BOOTTIME on Linux)
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

#include <fcntl.h>
#include <pthread.h>
#include <time.h>
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
  int churn_ms = 0;
  int seconds = 10;
  int io_kb = 0;
  std::string io_dir = "/tmp";
  int report_ms = 0;
};

std::mutex g_shared_mutex;      // the one hot lock
std::atomic<long> g_shared_counter{0};
std::atomic<bool> g_running{true};
std::atomic<long> g_iterations{0};
std::atomic<int> g_named_workers{0};

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

__attribute__((noinline)) void write_and_sync(int fd, const std::vector<char>& block,
                                              off_t* offset) {
  // A 4 MiB ring keeps the file bounded however long the run lasts.
  constexpr off_t kRing = 4 << 20;
  if (pwrite(fd, block.data(), block.size(), *offset) < 0) {
    std::perror("pwrite");
    std::exit(1);
  }
#if defined(__APPLE__)
  fsync(fd);
#else
  fdatasync(fd);
#endif
  *offset = (*offset + static_cast<off_t>(block.size())) % kRing;
}

long long clock_ns(clockid_t clock) {
  timespec ts{};
  clock_gettime(clock, &ts);
  return static_cast<long long>(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

long long report_clock_ns() {
#if defined(CLOCK_BOOTTIME)
  return clock_ns(CLOCK_BOOTTIME);
#else
  return clock_ns(CLOCK_MONOTONIC);
#endif
}

__attribute__((noinline)) void sleep_a_while(int sleep_us) {
  std::this_thread::sleep_for(std::chrono::microseconds(sleep_us));
}

void set_current_thread_name(const char* name) {
#if defined(__APPLE__)
  pthread_setname_np(name);
#else
  pthread_setname_np(pthread_self(), name);
#endif
}

__attribute__((noinline)) void short_lived_worker() {
  set_current_thread_name("short-lived");
  volatile long sink = burn_cpu(20);
  (void)sink;
}

__attribute__((noinline)) void worker_loop(const Options& opt, int index) {
  char name[16];
  std::snprintf(name, sizeof(name), "worker%d", index);
  set_current_thread_name(name);
  g_named_workers.fetch_add(1, std::memory_order_release);

  unsigned seed = static_cast<unsigned>(index * 2654435761u + 12345u);
  volatile long sink = 0;
  int io_fd = -1;
  off_t io_offset = 0;
  std::vector<char> io_block;
  if (opt.io_kb > 0) {
    std::string path = opt.io_dir + "/contention-io-XXXXXX";
    std::vector<char> templ(path.begin(), path.end());
    templ.push_back('\0');
    io_fd = mkstemp(templ.data());
    if (io_fd < 0) {
      std::perror("mkstemp");
      std::exit(1);
    }
    unlink(templ.data());
    io_block.assign(static_cast<size_t>(opt.io_kb) * 1024, static_cast<char>('a' + index % 26));
  }
  while (g_running.load(std::memory_order_relaxed)) {
    seed = seed * 1103515245u + 12345u;
    const int roll = static_cast<int>((seed >> 16) % 100);
    if (roll < opt.contention_pct) {
      hold_shared_lock(opt.hold_us);
    } else {
      sink += burn_cpu(opt.work_units);
    }
    if (io_fd >= 0) {
      write_and_sync(io_fd, io_block, &io_offset);
    }
    if (opt.sleep_us > 0) {
      sleep_a_while(opt.sleep_us);
    }
    g_iterations.fetch_add(1, std::memory_order_relaxed);
  }
  if (io_fd >= 0) {
    close(io_fd);
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
    else if (arg == "--churn-ms") { opt.churn_ms = int_arg(argc, argv, i++, "--churn-ms"); }
    else if (arg == "--seconds") { opt.seconds = int_arg(argc, argv, i++, "--seconds"); }
    else if (arg == "--io-kb") { opt.io_kb = int_arg(argc, argv, i++, "--io-kb"); }
    else if (arg == "--io-dir") {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "--io-dir needs a value\n");
        return 2;
      }
      opt.io_dir = argv[++i];
    }
    else if (arg == "--report-ms") { opt.report_ms = int_arg(argc, argv, i++, "--report-ms"); }
    else if (arg == "--help") {
      std::printf(
          "usage: contention [--threads N] [--contention PCT] [--hold-us U]\n"
          "                  [--sleep-us U] [--work N] [--churn-ms N]\n"
          "                  [--seconds S] [--io-kb N] [--io-dir PATH]\n"
          "                  [--report-ms N]\n");
      return 0;
    } else {
      std::fprintf(stderr, "unknown argument: %s\n", argv[i]);
      return 2;
    }
  }
  if (opt.threads < 1) opt.threads = 1;

  // Printed so a test harness can wait for readiness instead of sleeping and
  // hoping the threads exist yet.
  std::printf("pid %d threads %d contention %d%% hold %dus sleep %dus churn %dms\n",
              static_cast<int>(getpid()), opt.threads, opt.contention_pct,
              opt.hold_us, opt.sleep_us, opt.churn_ms);
  std::fflush(stdout);

  std::vector<std::thread> workers;
  workers.reserve(opt.threads);
  for (int i = 0; i < opt.threads; ++i) {
    workers.emplace_back(worker_loop, std::cref(opt), i);
  }
  while (g_named_workers.load(std::memory_order_acquire) < opt.threads) {
    std::this_thread::yield();
  }

  std::printf("ready\n");
  std::fflush(stdout);

  const auto started_at = std::chrono::steady_clock::now();
  auto next_churn = started_at;
  auto next_report = started_at;
  while (opt.seconds == 0 ||
         std::chrono::steady_clock::now() - started_at <
             std::chrono::seconds(opt.seconds)) {
    const auto now = std::chrono::steady_clock::now();
    if (opt.churn_ms > 0 && now >= next_churn) {
      std::thread transient(short_lived_worker);
      transient.join();
      next_churn = now + std::chrono::milliseconds(opt.churn_ms);
    }
    if (opt.report_ms > 0 && now >= next_report) {
      std::printf("tick %lld %ld %lld\n", report_clock_ns(), g_iterations.load(),
                  clock_ns(CLOCK_PROCESS_CPUTIME_ID));
      std::fflush(stdout);
      next_report = now + std::chrono::milliseconds(opt.report_ms);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  g_running.store(false, std::memory_order_relaxed);

  for (auto& worker : workers) {
    worker.join();
  }
  std::printf("iterations %ld\n", g_iterations.load());
  return 0;
}
