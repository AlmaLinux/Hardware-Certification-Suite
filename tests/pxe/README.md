Introduction
=

LTS - local testing server.
SUT - a system under tests.

To test the PXE, you need physical access to the SUT under test with a configured DHCP server on the LTS.
Both hosts must be on the same network.

PXE Server (LTS)
===
Prepare the server for system boot via PXE.

During the launch of the test, network parameters will be requested:
1. Subnet - subnet address
2. Netmask - netmask
3. Broadcast - broadcast address
4. From - from which ip address the range starts
5. To - on which ip the range ends
6. Server - ip address LTS
7. Install CL8 - if you want to install CL8 via PXE

Also, the parameters can be defined by default in the file ~/tests/pxe/vars.yml

PXE Client (SUT)
=

Check the PXE startup option in the BIOS menu and make sure it is enabled.

For example:

Press F2 (F11, DEL, etc...) during boot to enter BIOS setup  
|-- Select "Boot"  
|--- "Boot Device Priority"  
|---- Find "Network" in the list  

SUT must be started first of all through the network

if you see a window titled "PXE Boot Menu", the test has completed successfully.

Troubleshooting
=

Usually the problem is in the incorrect configuration of the DHCP server.  
You can find information about your network with the `ip a` command.