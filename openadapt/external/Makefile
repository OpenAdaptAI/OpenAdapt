all: qsysinfo vwin

clean:
	del bin/*.exe

qsysinfo:
	gcc -O3 qsysinfo.c -o bin/qsysinfo.exe -lntdll -lShlwapi

vwin:
	gcc -O3 vwin.c -o bin/vwin.exe
