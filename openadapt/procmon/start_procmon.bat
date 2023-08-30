@echo off
set LOG_PATH=C:\Users\avide\OneDrive\Desktop\Coding Proj\MLDSAI\NewFork\OpenAdapt\openadapt\procmon\test.pml
del "%LOG_PATH%"
start C:\Users\avide\Downloads\ProcessMonitor\procmon.exe /Minimized /AcceptEula /LoadConfig "C:\Users\avide\OneDrive\Desktop\Coding Proj\MLDSAI\NewFork\OpenAdapt\openadapt\procmon\ProcmonConfiguration.pmc" /BackingFile "%LOG_PATH%"
