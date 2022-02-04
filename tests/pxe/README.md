Introduction
=

LTS - local testing server.
SUT - a system under tests.

To test the PXE, you need physical access to the machine under test with a configured DHCP server on the local network.

PXE Client (SUT)
=

Check the PXE startup option in the BIOS menu and make sure it is enabled.

For example:

Press F2 during boot to enter BIOS setup
|-- Select "Boot"
|--- "Boot Device Priority"
|---- Find "Network" in the list

SUT must be started first of all through the network

PXE Server (LTS)
===
Prepare the server for system boot via PXE.

**Way first (not verified)**

Clone repository `https://github.com/ferrarimarco/docker-pxe`
Run command:  
`docker run --cap-add=NET_ADMIN -it --rm --net=host ferrarimarco/pxe`