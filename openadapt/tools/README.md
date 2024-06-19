# tools
This directory contains various tools that can be run standalone from OpenAdapt. There are precompiled binaries for Windows in the `bin` directory, and the source code for each tool is in the root of this directory.

## vwin.c 
This tool enumerates all open windows on the system and prints details such as window title, process ID, start time, and start date.

To compile, run:
```
make vwin
```

## qsysinfo.c
This tool serves as an example of how to use `NtQuerySystemInformation` to print various types process information, such as
process ID, parent process ID, creation time, memory usage, and more.

To compile, run:
```
make qsysinfo
```

### To compile all tools
```
make all
```

### To delete all compiled tools
```
make clean
```
