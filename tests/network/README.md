## Stress testing network
### Environments requirements
Must have 2 servers on the same network. LTS - local testing server and SUT - a system under tests. Servers must use en_US locale (parsing command output).
Every network port must be cabled to the LAN and properly configured with either DHCP or static addressing. If a SUT has 4 NIC ports, then all 4 must be connected to the LAN.
The network test uses the iperf3 tool.
### Test algorithm
1. Using the lshw tool, the test detects all network devices.
2. One device is selected in turn for testing, all others are temporarily disabled.
3. Using ethtool for each device, the maximum speed is defined.
4. Next on LTS starts the iperf3 server and on SUT runs the iperf3 client for 4 hours.
5. The client and server continuously exchange packets and measure the transfer rate.

After the end of the test, the average data transfer rate and the maximum device speed are compared, and the percentage deviation from the maximum is calculated. The test is considered passed if the speed during the test did not drop below 80% and the average speed is above 80%.
### Running test automatically
1. Execute ansible role “/network/roles/main”
### Manual running test
1. On LTS and SUT run: yum install iperf3 sshpass ethtool net-tools
2. On LTS run:
   1. For all network devices: `/network/run_test.sh <LTS-host> <SUT-host> [test-time-in-minuts] [target-network-speed-in-Mbps]`
   2. For specific network device: `/network/run_test.sh <LTS-host> <SUT-host> [test-time-in-minuts] [target-network-speed-in-Mbps] [network-device]`
3. On LTS and SUT run: yum remove iperf3 sshpass ethtool net-tools
### StdOut example
At the end test prints one of them:
1. Test SUCCESS!
2. Test FAILED!
And info about log file.

## Extended result
The extended result can be found in a log file.

The results contain a table split by 60-sec intervals with bandwidth statistics and average data in the footer.