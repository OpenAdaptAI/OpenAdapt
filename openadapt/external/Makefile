all: qsysinfo vwin

clean:
	del *.exe

qsysinfo:
	gcc -O3 qsysinfo.c -o qsysinfo.exe -lntdll -lShlwapi

vwin:
	gcc -O3 vwin.c -o vwin.exe
