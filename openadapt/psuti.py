import cProfile
import psutil

def profile_code():
    # Get all open files from all processes through psutil
    for proc in psutil.process_iter():
        try:
            for item in proc.open_files():
                print(item)
        except Exception as e:
            print(e)

profile = False

if profile:
    # Profile the code
    profiler = cProfile.Profile()
    profiler.runcall(profile_code)

    # Print the statistics, sorted by cumulative time
    profiler.print_stats(sort="cumulative")

    # Or save the statistics to a file
    profiler.dump_stats("profile_results.prof")


    import pstats

    stats = pstats.Stats("profile_results.prof")
    stats.strip_dirs() # Clean up filenames
    stats.sort_stats("cumulative") # Sort by cumulative time
    stats.print_stats() # Print the sorted statistics
else:
    profile_code()