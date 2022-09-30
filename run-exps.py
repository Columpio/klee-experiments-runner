from collections import defaultdict
import abc
import os
import shutil
import sys
import subprocess
import difflib
import datetime


def log(*args, **kwargs):
  kwargs["flush"] = True
  print(*args, **kwargs)


class SymArgs:
  def __init__(self, sym_args_list, sym_files=None, sym_stdin=None):
    self.sym_args_list = sym_args_list
    self.sym_files = sym_files
    self.sym_stdin = sym_stdin

  def argument_list(self):
    args = [f"--sym-args {' '.join(map(str, sym_args))}" for sym_args in self.sym_args_list]
    if self.sym_files is not None:
      args.append(f"--sym-files {self.sym_files[0]} {self.sym_files[1]}")
    if self.sym_stdin is not None:
      args.append(f"--sym-stdin {self.sym_stdin}")
    args.append("--sym-stdout")
    return args


class KLEE:
  KLEE_PATH = "/home/columpio/klee"
  MAX_SOLVER_TIME = "15s"
  MAX_TIME = 60
  WAIT_UNTIL_KILL_SCALE = 2
  BENCHMARKS_PATH = "/tmp"
  SANDBOX = "/tmp/sandbox"
  BUILD_FOLDER = "build-mydev"
  BINARY_KLEE = os.path.join(KLEE_PATH, BUILD_FOLDER, "bin", "klee")
  BINARY_KLEE_STATS = os.path.join(KLEE_PATH, BUILD_FOLDER, "bin", "klee-stats")
  KLEE_STATS_DIFF = "klee_stats_diff"
  SYM_ARGS = defaultdict(lambda: SymArgs([(0, 1, 10), (0, 2, 2)], (1, 8), 8),
    { # Taken from: https://klee.github.io/docs/coreutils-experiments/
      "dd": SymArgs([(0, 3, 10)], (1, 8), 8),
      "dircolors": SymArgs([(0, 3, 10)], (2, 12), 12),
      "echo": SymArgs([(0, 4, 300)], (2, 30), 30),
      "expr": SymArgs([(0, 1, 10), (0, 3, 2)]),
      "mknod": SymArgs([(0, 1, 10), (0, 3, 2)], (1, 8), 8),
      "od": SymArgs([(0, 3, 10)], (2, 12), 12),
      "pathchk": SymArgs([(0, 1, 2), (0, 1, 300)], (1, 8), 8),
      "printf": SymArgs([(0, 3, 10)], (2, 12), 12)
  })
  def __init__(self, name, flags=None):
    if flags is None:
      flags = []
    self.flags = flags
    self.name = name

  @staticmethod
  def output_base_dir(benchmark_path):
    return benchmark_path + "-klee-out"

  def output_dir(self, output_base_dir):
    return os.path.join(output_base_dir, self.name)

  def sym_flags(self, benchmark_name):
    tool_name = os.path.splitext(benchmark_name)[0]
    return KLEE.SYM_ARGS[tool_name].argument_list()

  def command(self, benchmark_entry):
    output_base_dir = KLEE.output_base_dir(benchmark_entry.path)
    output_dir = self.output_dir(output_base_dir)
    os.makedirs(output_dir, exist_ok=True)
    shutil.rmtree(output_dir)
    shutil.rmtree(KLEE.SANDBOX, ignore_errors=True)
    os.makedirs(KLEE.SANDBOX)
    basic_flags = [
      KLEE.BINARY_KLEE,
      "--simplify-sym-indices",
      "--max-memory=1000",
      "--optimize",
      "--libc=uclibc",
      "--posix-runtime",
      "--external-calls=all",
      "--only-output-states-covering-new",
      f"--env-file={os.path.join(KLEE.BENCHMARKS_PATH, 'test.env')}",
      f"--run-in-dir={KLEE.SANDBOX}",
      "--max-sym-array-size=4096",
      f"--max-solver-time={KLEE.MAX_SOLVER_TIME}",
      f"--max-time={KLEE.MAX_TIME}",
      "--watchdog",
      "--max-static-fork-pct=1",
      "--max-static-solve-pct=1",
      "--max-static-cpfork-pct=1",
      "--switch-type=internal",
      f"--output-dir={output_dir}"
    ]
    return basic_flags + self.flags + [benchmark_entry.path] + self.sym_flags(benchmark_entry.name)

  def run(self, benchmark_entry):
    command = ' '.join(self.command(benchmark_entry))
    log(f"Running {command}")
    proc = subprocess.Popen(command, shell=True, stdout=sys.stdout, stderr=sys.stderr, cwd=KLEE.BENCHMARKS_PATH)
    try:
      proc.wait(timeout=KLEE.MAX_TIME * KLEE.WAIT_UNTIL_KILL_SCALE)
      log(f"Process finished under {KLEE.WAIT_UNTIL_KILL_SCALE} * time limit")
    except subprocess.TimeoutExpired:
      log(f"Process not finished under {KLEE.WAIT_UNTIL_KILL_SCALE} * time limit, so killing it")
      proc.kill()
      proc.wait()


class Differ(abc.ABC):
  DIFF_EXT = ".patch"
  @abc.abstractmethod
  def read_originals(self, output_base_dir, out1, out2):
    raise Exception("Abstract method")

  def save_diff(self, output_base_dir, out1, out2):
    info1_lines, info2_lines, info1, info2, info_out = self.read_originals(output_base_dir, out1, out2)
    diff = difflib.unified_diff(info1_lines, info2_lines, fromfile=info1, tofile=info2, n=3)
    with open(info_out + Differ.DIFF_EXT, 'w') as out:
      out.writelines(diff)


class InfoFilesDiffer(Differ):
  def read_originals(self, output_base_dir, out1, out2):
    info_out, info1, info2 = map(lambda base: os.path.join(base, "info"), [output_base_dir, out1, out2])
    with open(info1) as info1_file:
      info1_lines = info1_file.readlines()
    with open(info2) as info2_file:
      info2_lines = info2_file.readlines()
    return info1_lines, info2_lines, info1, info2, info_out


class KleeStatsDiffer(Differ):
  def call_klee_stats_on(self, path):
    proc = subprocess.run([KLEE.BINARY_KLEE_STATS, path], capture_output=True, encoding="utf-8")
    out = proc.stdout.splitlines(keepends=True)
    return out

  def read_originals(self, output_base_dir, out1, out2):
    info_out = os.path.join(output_base_dir, KLEE.KLEE_STATS_DIFF)
    info1_lines = self.call_klee_stats_on(out1)
    info2_lines = self.call_klee_stats_on(out2)
    return info1_lines, info2_lines, f"klee-stats {out1}", f"klee-stats {out2}", info_out


class ResultComparator:
  def __init__(self, klee1 : KLEE, klee2 : KLEE, differ : Differ):
    self.klee1 = klee1
    self.klee2 = klee2
    self.differ = differ

  def save_diff(self, benchmark_entry):
    output_base_dir = KLEE.output_base_dir(benchmark_entry.path)
    out1 = self.klee1.output_dir(output_base_dir)
    out2 = self.klee2.output_dir(output_base_dir)
    self.differ.save_diff(output_base_dir, out1, out2)


def print_estimated_time_left(benchmarks_left):
  def human_readable_time(seconds):
    return (datetime.datetime.min + datetime.timedelta(seconds=seconds)).strftime("%H hours, %M minutes, %S seconds")
  NUMBER_OF_COMPARED_KLEES = 2
  estimated_time_left = benchmarks_left * KLEE.MAX_TIME * NUMBER_OF_COMPARED_KLEES
  max_time_left = estimated_time_left * KLEE.WAIT_UNTIL_KILL_SCALE
  estimated_time_left = human_readable_time(estimated_time_left)
  max_time_left = human_readable_time(max_time_left)
  print(f"ESTIMATED TIME LEFT: {estimated_time_left} (no more than {max_time_left})")


def has_both_solutions(benchmark):
  return os.path.exists(os.path.join(KLEE.output_base_dir(benchmark.path), KLEE.KLEE_STATS_DIFF + Differ.DIFF_EXT))


def main():
  KLEE.BENCHMARKS_PATH = "/home/columpio/coreutils/obj-llvm/src"
  LOGGER = open(os.path.join(KLEE.BENCHMARKS_PATH, "run-exps.log"), 'w')
  sys.stdout = LOGGER
  sys.stderr = LOGGER
  benchmarks = [entry for entry in os.scandir(KLEE.BENCHMARKS_PATH) if not entry.name.startswith(".") and entry.name.endswith(".bc")]
  bd_klee = KLEE("No___Blacklist", flags=["--disable-blacklist"])
  my_klee = KLEE("With_Blacklist")
  result_comparator = ResultComparator(bd_klee, my_klee, KleeStatsDiffer())
  benchmarks_len = len(benchmarks)
  for i, benchmark in enumerate(benchmarks):
    if has_both_solutions(benchmark):
      continue
    print_estimated_time_left(benchmarks_len - i)
    bd_klee.run(benchmark)
    my_klee.run(benchmark)
    result_comparator.save_diff(benchmark)
  LOGGER.close()


if __name__ == "__main__":
  main()
