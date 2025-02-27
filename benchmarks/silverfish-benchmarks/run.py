import csv
import os
import subprocess as sp
import sys
import timeit
import numpy as np

# CSV file name
CSV_NAME = "benchmarks.csv"

# Absolute path to the `code_benches` directory
BENCH_ROOT = os.getcwd()
# Absolute path to the `silverfish` directory
ROOT_PATH = os.path.dirname(BENCH_ROOT)

# Lucet WASM compilation and runtime.
WASM_CLANG = "wasm32-wasi-clang"
LUCETC = "lucetc-wasi"
LUCET  = "lucet-wasi"
# For some reason, LUCET specification says --min-researved-size is 4MB and --max-reserved-size is 4GB but 
# the reality is it sets the reserved size of the WASM module to be 4MB and does not expand resulting in
# HeapOutOfBounds in some applications that are memory hungry!
#
# --max-reserved-size param in lucetc-wasi is totally ignored, so anything we set in --min-reserved-size or --reserved-size
# seem to be what it just goes with!
RESERVED_HEAP = "32MiB"

# How many times should we run our benchmarks
RUN_COUNT = 100
ENABLE_DEBUG_SYMBOLS = True


# FIXME: Mibench runs many of these programs multiple times, which is probably worth replicating
class Program(object):
    def __init__(self, name, parameters, stack_size, custom_arguments=None, do_lto=True):
        self.name = name
        self.parameters = parameters
        self.stack_size = stack_size
        self.custom_arguments = ""
        if custom_arguments:
            self.custom_arguments = " ".join(custom_arguments)
        self.do_lto = do_lto

    def __str__(self):
        return "{}({})".format(self.name, " ".join(map(str, self.parameters)))


# These are the programs we're going to test with
# TODO: Fix ispell, which doesn't compile on OS X
# TODO: Fix sphinx, which doesn't work properly on OS X
# TODO: Do ghostscript, which has a ton of files
programs = [
    # Real world program benchmarks
    # FIXME: Does not work because Lucet-WASI does not support tmpfile()
    # Program("libjpeg", [], 2 ** 15,
    #         custom_arguments=["-Wno-incompatible-library-redeclaration", "-Wno-implicit-function-declaration", "-Wno-shift-negative-value"]),

    # FIXME: Does not work because it depends on pthreads and Lucet-WASI is single-threaded only 
    # (wasi-sdk is forced to be single thread!).
    # Missing support for threads: https://github.com/CraneStation/wasi-libc/tree/master/libc-top-half
    # Program("sqlite", [], 2 ** 15),

    # Synthetic benchmarks
    Program("binarytrees", [16], 2 ** 14),
    Program("function_pointers", [], 2 ** 14),
    Program("matrix_multiply", [], 2 ** 14),

    # Benchmark programs
    Program("adpcm", ["< ./large.pcm"], 2 ** 14,
            custom_arguments=["-Wno-implicit-int", "-Wno-implicit-function-declaration"]),
    Program("basic_math", [], 2 ** 14),
    Program("bitcount", [2 ** 24], 2 ** 14),
    Program("crc", ["./large.pcm"], 2 ** 14, custom_arguments=["-Wno-implicit-int", "-Wno-format"]),
    Program("dijkstra", ["./input.dat"], 2 ** 14,
            custom_arguments=["-Wno-return-type"]),
    Program("fft", [8, 32768], 2 ** 14),

    # FIXME: After fixing errno problem with -DHAS_ERRNO_DECL,
    # LucetcError { inner: ErrorMessage { msg: "Unknown module for symbol `env::signal`" }
    # From what I read about wasi-libc (based on muslc), does not support signals yet.
    # https://github.com/CraneStation/wasi-libc/tree/master/libc-top-half
    # Program("gsm", ["-fps", "-c", "./large.au"], 2 ** 15, custom_arguments=["-DSASR", "-Wno-everything", "-DHAS_ERRNO_DECL"]),

    Program("mandelbrot", [5000], 2 ** 14),
    Program("patricia", ["./large.udp"], 2 ** 14),

    # FIXME: Even with --reserved-size 4GiB, it shows HeapOutOfBounds! I don't understand what is wrong here!
    # Program("qsort", ["./input_small.dat"], 2 ** 18),
    Program("rsynth", ["-a", "-q", "-o", "/dev/null", "< ./largeinput.txt"], 2**14,
            custom_arguments=["-I.", "-Wno-everything", "-I/usr/local/include/"]),
    Program("sha", ["./input_large.asc"], 2 ** 14),

    # FIXME: Even with --reserved-size 4GiB, it shows HeapOutOfBounds! I don't understand what is wrong here!
    # Program("susan", ["./input_large.pgm", "./bin/output.txt", "-s"], 2 ** 19, custom_arguments=["-Wno-everything"]),
    Program("stringsearch", [], 2 ** 13),

    # NOTE: Modified output to use a regular file instead of /dev/null and it worked for Lucet!
    Program("blowfish", ["e", "./input_large.asc", "./bin/output.txt", "1234567890abcdeffedcba0987654321"], 2**14),

    # TODO: Uses signals and that is not supported in Lucet-WASI
    # Program("pgp", ['-sa -z "this is a test" -u taustin@eecs.umich.edu testin.txt austin@umich.edu'], 2 ** 14,
    #         custom_arguments=["-DUNIX -D_BSD -DPORTABLE -DUSE_NBIO -DMPORTABLE", "-I.", "-Wno-everything"]),
]


# Compile the C code in `program`'s directory into a native executable
def compile_to_executable(program):
    opt = "-O3"
    if program.do_lto:
        opt += " -flto"
    if ENABLE_DEBUG_SYMBOLS:
        opt += " -g"
    sp.check_call("clang {} -lm -lpthread -ldl {o} *.c -o bin/{p}".format(program.custom_arguments, o=opt, p=program.name), shell=True, cwd=program.name)


# Compile the C code in `program`'s directory into WASM
def compile_to_wasm(program):
    flags = "" #WASM_FLAGS.format(stack_size=program.stack_size)
    command = "{clang} {flags} {args} -I. -O3 -flto *.c -o bin/{pname}.wasm" \
        .format(clang=WASM_CLANG, flags=flags, args=program.custom_arguments, pname=program.name)
    sp.check_call(command, shell=True, cwd=program.name)


# Compile the WASM in `program`'s directory into Lucet-C binary 
def compile_wasm_to_bc(program):
    command = "{lucetc} --opt-level 2 bin/{pname}.wasm --output bin/{pname}.so --reserved-size {heap}".format(lucetc=LUCETC, pname=program.name, heap=RESERVED_HEAP)
    sp.check_call(command, shell=True, cwd=program.name)


# Execute executable `p` with arguments `args` in directory 'dir'
def execute_wasm(p, args, dir):
    command = "{lucet} --dir .:. {pname} -- {a}".format(lucet=LUCET, pname=p, a=args)
    sp.check_call(command, shell=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL, cwd=dir)


# Execute wasi binary 'p' with arguments 'args' in directory 'dir'
def execute_native(p, args, dir):
    command = p + " " + args
    sp.check_call(command, shell=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL, cwd=dir)


# Benchmark the given program's executable
#   p = the program
#   name = the human readable name for this version of the executable
def bench_native(p, name):
    command = "execute_native('./bin/{pname}', '{args}', '{dir}')".format(pname=p.name, args=' '.join(map(str, p.parameters)), dir=p.name)
    return timeit.repeat(command, 'from __main__ import execute_native', number=1, repeat=RUN_COUNT)


# Benchmark the given program's executable
#   p = the program
#   name = the human readable name for this version of the executable
def bench_wasm(p, name):
    command = "execute_wasm('./bin/{pname}.so', '{args}', '{dir}')".format(pname=p.name, args=' '.join(map(str, p.parameters)), dir=p.name)
    return timeit.repeat(command, 'from __main__ import execute_wasm', number=1, repeat=RUN_COUNT)


# Format row information for a run and output average execution time and how much faster or slower it is compared to native
# row = row data structure
# vals = execution times for RUN_COUNT iterations of the current execution model
# base_avg = average execution time for native execution
def format_run(row, vals, base_avg):
    curr_avg = round(np.average(vals), 4)
    base_avg = round(base_avg, 4)
    percent  = 0
    relation = "slower"

    if curr_avg > base_avg:
        percent = ((curr_avg - base_avg) / base_avg) * 100
    else:
        percent = ((base_avg - curr_avg) / base_avg) * 100
        relation = "faster"
    print(" {:.4f} ({:.2f}% {})".format(curr_avg, percent, relation))

    #slowdown/speedup, average, 99th percentile, 95th percentile, minimum(best-case), maximum(worst-case), standard-deviation
    row.append("{:.2f}% {}".format(percent, relation))
    row.append("{:.4f}".format(np.average(vals)))
    row.append("{:.4f}".format(np.percentile(vals, 99)))
    row.append("{:.4f}".format(np.percentile(vals, 95)))
    row.append("{:.4f}".format(np.amin(vals)))
    row.append("{:.4f}".format(np.amax(vals)))
    row.append("{:.4f}".format(np.std(vals)))


# Compile all our programs
for i, p in enumerate(programs):
    print("Compiling {} {}/{}".format(p.name, i + 1, len(programs)))

    os.makedirs(p.name + "/bin", exist_ok=True)
    print("==> NATIVE")
    compile_to_executable(p)
    print("==> WASM")
    compile_to_wasm(p)
    compile_wasm_to_bc(p)

print()
print("Test Iterations: %d" % RUN_COUNT)
print("Outputting benchmark data to " + CSV_NAME)
print()

with open(CSV_NAME, 'w+', newline='') as csv_file:
    csv_writer = csv.writer(csv_file)

    columns = ["Program", "Iterations", 
            "native", "avg", "99th %-tile", "95th %-tile", "min", "max", "sd", 
            "wasm", "avg", "99th %-tile", "95th %-tile", "min", "max", "sd"]
    csv_writer.writerow(columns)

    # Benchmark and output timing info for each of our programs
    for p in programs:
        csv_row = [p.name]

        csv_row.append(RUN_COUNT)
        print("Executing", p.name)
        print("==> NATIVE", end='', flush=True)
        base_speed = bench_native(p, "native")
        format_run(csv_row, base_speed, np.average(base_speed))

        print("==> WASM", end='', flush=True)
        lucet_speed = bench_wasm(p, "wasm")
        format_run(csv_row, lucet_speed, np.average(base_speed))
        #csv_row.append(0, 0, 0, 0, 0, 0, 0)

        csv_writer.writerow(csv_row)
        print("")
