@echo off
:: Path to your ProcMon executable
set PROCMON_PATH=C:\Users\avide\Downloads\ProcessMonitor\procmon.exe

:: Paths for input .pml and output .csv files
set INPUT_PML=C:\Users\avide\OneDrive\Desktop\Coding Proj\MLDSAI\NewFork\OpenAdapt\openadapt\procmon\test.pml
set OUTPUT_CSV=C:\Users\avide\OneDrive\Desktop\Coding Proj\MLDSAI\NewFork\OpenAdapt\openadapt\procmon\test.csv

:: Run ProcMon to do the conversion
"%PROCMON_PATH%" /OpenLog "%INPUT_PML%" /SaveAs "%OUTPUT_CSV%"
