## Phoronix test suits
The test can be run via ansible only with tag "phoronix" or manual.
The test installs and runs the Phoronix test suite with the following test suites:
- database
- server
- server-memory
- disk
- kernel
- single-threaded

After the end of the tests, PDF and JSON reports are generated and copied to the logs folder.
### Running test automatically
On LTS run: `ansible-playbook -i <SUT_IP>, automated.yml --tags phoronix`
### Manual running test
Instal all dependencies on SUT: 

`yum install phoronix-test-suite uuid-devel snappy-devel gflags-devel erlang opencl-headers glfw-devel yasm nasm p7zip p7zip-plugins meson opencv-devel google-benchmark-devel --enablerepo=powertools,epel-playground,epel`

Noninteractive mode:

On SUT run: `phoronix-test-suite batch-benchmark database server server-memory disk kernel single-threaded`

Interactive mode:

On SUT run: `phoronix-test-suite benchmark database server server-memory disk kernel single-threaded`

Debug mode: 

On SUT run: `phoronix-test-suite debug-benchmark  database server server-memory disk kernel single-threaded`

### Manual convert reports to PDF

Show results:

`ls -1 /var/lib/phoronix-test-suite/test-results/` or `phoronix-test-suite list-saved-results`

Conver to PDF:

`phoronix-test-suite result-file-to-pdf <result-name>`

Convert to JSON:

`phoronix-test-suite result-file-to-json <result-name>`
## Extended result
The extended result and PDF reports can be found in a log files directory.