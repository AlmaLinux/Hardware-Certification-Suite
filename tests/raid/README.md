## Raid testing
### Test algorithm
1. Test looks in /proc/mdstat for information about RAID.
2. If RAID is detected, using fio utility starts storage stress testing for 4 hours.
3. During storage stress testing, using mdadm utility checks RAID status (every ~20 seconds).
4. The test is passed if at the end of testing RAID status is good.
### Running test automatically
1. On LTS and SUT execute ansible role “/raid/roles/setup”
2. On LTS execute ansible role: /raid/roles/test
3. On LTS and SUT execute ansible role “/raid/roles/cleanup”
### Manual running test
1. On LTS and SUT run: yum install mdadm fio
2. On LTS run: /raid/run_test.sh
3. On LTS and SUT run: yum remove mdadm fio
### StdOut example
At the end test prints one of them:
1. Test SUCCESS!
2. Test FAILED!
And info about log file.

## Extended result
The extended result can be found in a log file.