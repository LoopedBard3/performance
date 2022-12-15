import os
import requests
import subprocess
from performance.logger import setup_loggers
from shared.precommands import PreCommands
from shared import const

setup_loggers(True)
precommands = PreCommands()

if not os.path.exists('./maui'):
    subprocess.run(['git', 'clone', 'https://github.com/dotnet/maui.git', '--single-branch', '--depth', '1'])
    subprocess.run(['powershell', '-Command', r'Remove-Item -Path .\\maui\\.git -Recurse -Force']) # Git files have permission issues, for their deletion seperately

# This part needs to be worked out as the precommands build is failing, but external build succeeds (dotnet build src/Core/tests/Benchmarks.Droid/Benchmarks.Droid.csproj -t:Benchmark -c Release) TODO Fix that/figure out the failure reason
precommands.existing(projectdir='./maui',projectfile='./src/Core/tests/Benchmarks.Droid/Benchmarks.Droid.csproj')

# Build the APK
precommands.execute(['-t:Benchmark'])

# Commands run to transfer the files back to device
# adb shell 'run-as com.microsoft.maui.benchmarks cp -r files /sdcard/Benchmarks'
# cd ""Where you want to store the benchmark files""
# adb pull /sdcard/Benchmarks
# adb shell rm -r /sdcard/Benchmarks
# ""Uninstall the app"" adb uninstall com.microsoft.maui.benchmarks

# After the test, we need to upload the results using upload.py
# Something like this:
# if runninginlab():
#     copytree(TRACEDIR, os.path.join(helixuploaddir(), 'traces'))
#     if uploadtokenpresent():
#         import upload
#         upload_code = upload.upload(self.reportjson, upload_container, UPLOAD_QUEUE, UPLOAD_TOKEN_VAR, UPLOAD_STORAGE_URI)
#         getLogger().info("Startup Upload Code: " + str(upload_code))
#         if upload_code != 0:
#             sys.exit(upload_code)