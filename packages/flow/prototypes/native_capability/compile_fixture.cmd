@echo off
setlocal
if "%~1"=="" exit /b 90
if "%~2"=="" exit /b 91
if "%~3"=="" exit /b 96
set "CL="
set "_CL_="
set "LINK="
set "_LINK_="
rem Existing compiler environment in this child only; no installation/settings.
call "%~1" %~2 > "%~dp0out\vcvars.log" 2>&1
if errorlevel 1 exit /b 92
if not "%WindowsSDKVersion%"=="%~2\" exit /b 93
if not "%VCToolsVersion%"=="14.40.33807" exit /b 94
> "%~dp0out\compiler-path.txt" echo %~3
> "%~dp0out\toolchain-selection.txt" echo SDK=%WindowsSDKVersion%
>> "%~dp0out\toolchain-selection.txt" echo MSVC=%VCToolsVersion%
pushd "%~dp0out"
"%~3" /Bv /std:c++17 /EHsc /W4 /MT /Fe:fixture.exe "..\sdk_capability.cpp" "..\abi_fixture.cpp" /link /INCREMENTAL:NO /SUBSYSTEM:CONSOLE ole32.lib oleaut32.lib
set "FIXTURE_BUILD_RESULT=%ERRORLEVEL%"
popd
exit /b %FIXTURE_BUILD_RESULT%
