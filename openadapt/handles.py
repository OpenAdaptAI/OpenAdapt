# import subprocess

# def get_open_files():
#     result = subprocess.run(['handle.exe'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
#     if result.returncode != 0:
#         print(f"Error running handle.exe: {result.stderr}")
#         return []

#     # Extracting the files from the output
#     files = []
#     for line in result.stdout.split('\n'):
#         if 'File' in line:
#             files.append(line.split(' ')[-1].strip())

#     return files

# open_files = get_open_files()
# for file in open_files:
#     print(file)


import subprocess
import psutil
import timeit

def get_open_files_handle():
    result = subprocess.run(['handle.exe'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error running handle.exe: {result.stderr}")
        return []

    # Extracting the files from the output
    files = []
    for line in result.stdout.split('\n'):
        if 'File' in line:
            files.append(line.split(' ')[-1].strip())

    return files

def get_open_files_handle64():
    result = subprocess.run(['handle64.exe'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"Error running handle.exe: {result.stderr}")
        return []

    # Extracting the files from the output
    files = []
    for line in result.stdout.split('\n'):
        if 'File' in line:
            files.append(line.split(' ')[-1].strip())

    return files

def get_open_files_psutil():
    files = []
    for process in psutil.process_iter(attrs=['pid', 'open_files']):
        open_files = process.info.get('open_files')
        if open_files:
            for file in open_files:
                files.append(file.path)
    return files

def time_functions():
    handle_time = timeit.timeit("get_open_files_handle()", setup="from __main__ import get_open_files_handle", number=1)
    print(f"Time taken using handle.exe: {handle_time} seconds")

    psutil_time = timeit.timeit("get_open_files_psutil()", setup="from __main__ import get_open_files_psutil", number=1)
    print(f"Time taken using psutil: {psutil_time} seconds")

if __name__ == "__main__":
    time_functions()
