@echo off
rem ---------------------------------------------------------------
rem  Build the cartridge and start it in Classic99.
rem
rem  Set CLASSIC99 to the folder holding classic99.exe, or drop the
rem  emulator in ..\classic99 next to this repository.
rem ---------------------------------------------------------------

python tools\build.py || goto :end

if "%CLASSIC99%"=="" set CLASSIC99=%~dp0..\classic99
if not exist "%CLASSIC99%\classic99.exe" (
    echo Could not find classic99.exe in "%CLASSIC99%".
    echo Get it from https://github.com/tursilion/classic99 ^(dist\classic99.zip^)
    echo and set the CLASSIC99 environment variable to the folder.
    goto :end
)

copy /y build\jawbreaker2-8.bin "%CLASSIC99%\jawbreaker2-8.bin" > nul
pushd "%CLASSIC99%"
start "" classic99.exe -rom jawbreaker2-8.bin
popd
echo Started Classic99. Press a key on the title screen, then 2 for JAWBREAKER II.

:end
