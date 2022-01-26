Introduction
=
Testing the operation of the CPU with a high load.

A general stress test is performed to verify that the system can handle a sustained high load for a period of time.

This utilizes the test tool “stress-ng”.

CPU testing
=
Install stress-ng:  
`yum --enablerepo=epel install stress-ng`

Run command: 
`sh run_test.sh 4h`

Parameters:

`$1` - By default, the stress test will stop after 4 hours.  
You can specify time units in seconds, minutes, hours, days, or years with the s, m, h, d, or y suffix. If the timeout is 0, the test will run forever.

For example:
* `sh run_test.sh 15s`
* `sh run_test.sh 60m`
* `sh run_test.sh 4h`
* `sh run_test.sh 2d`
* `sh run_test.sh 1y`

The output will display information about the test and the result.

List of errors (stderr):
1. Error; incorrect user options or a fatal resource issue in the stress-ng stressor harness (for example, out of memory).
2. One or more stressors failed.
3. One or more stressors failed to initialise because of lack of resources, for example ENOMEM (no memory), ENOSPC (no space on file system) or a missing or unimplemented system call.
4. One or more stressors were not implemented on a specific architecture or operating system.
5. A stressor has been killed by an unexpected signal.
6. A stressor exited by exit(2) which was not expected and timing metrics could not be gathered.
7. The bogo ops metrics maybe untrustworthy. This is most likely to occur when a stress test is terminated during the update of a bogo-ops counter such as when it has been OOM killed. A less likely reason is that the counter ready indicator has been corrupted.
