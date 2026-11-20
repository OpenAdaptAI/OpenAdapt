@echo off
setlocal
if "%~1"=="" exit /b 90
if "%~2"=="" exit /b 91
if "%~3"=="" exit /b 92
set "CL="
set "_CL_="
set "LINK="
set "_LINK_="
call "%~1" %~2 > "%~dp0..\out\vcvars.log" 2>&1
if errorlevel 1 exit /b 93
if not "%WindowsSDKVersion%"=="%~2\" exit /b 94
if not "%VCToolsVersion%"=="14.40.33807" exit /b 95
> "%~dp0..\out\compiler-path.txt" echo %~3
> "%~dp0..\out\toolchain-selection.txt" echo SDK=%WindowsSDKVersion%
>> "%~dp0..\out\toolchain-selection.txt" echo MSVC=%VCToolsVersion%
pushd "%~dp0..\out"
"%~3" /Bv /std:c++17 /EHsc /W4 /MT /Fe:provider_fixture.exe "..\sdk_capability.cpp" "..\provider_source\sdk_checkpoint.cpp" "..\provider_source\provider_fixture.cpp" /link /INCREMENTAL:NO /SUBSYSTEM:CONSOLE ole32.lib oleaut32.lib user32.lib uiautomationcore.lib
set "PROVIDER_BUILD_RESULT=%ERRORLEVEL%"
popd
exit /b %PROVIDER_BUILD_RESULT%
