import subprocess
import os
import time

def start_procmon():
    LOG_PATH = "C:\\Users\\avide\\Documents\\Work\\MLDSAI\\puterbot\\openadapt\\procmon\\test.pml"
    PROCMON_PATH = "C:\\Users\\avide\\Downloads\\ProcessMonitor\\procmon64.exe"
    CONFIG_PATH = "C:\\Users\\avide\\OneDrive\\Desktop\\Coding Proj\\MLDSAI\\NewFork\\OpenAdapt\\openadapt\\procmon\\ProcmonConfiguration.pmc"

    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)

    command = [PROCMON_PATH, "/Minimized", "/AcceptEula", "/BackingFile", LOG_PATH]
    proc = subprocess.Popen(command)  # Use Popen here
    return proc

def kill_procmon(proc):
    try:
        proc.terminate()  # Send a terminate signal to the process
    except Exception as e:
        print(f"Error terminating ProcMon: {e}")

if __name__ == '__main__':
    procmon_process = start_procmon()
    # ... your code ...
    print("Sleeping for 10 seconds...")
    time.sleep(10)
    print("Killing the process...")
    kill_procmon(procmon_process)
