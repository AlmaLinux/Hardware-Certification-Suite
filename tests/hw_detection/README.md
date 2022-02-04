###Hardware detection report
script: hw_detection.sh

example of command: `ansible-playbook -i 10.51.32.16, main.yml -u root --tags hw_detection`

Example:

```shell
System Hardware Components report

BIOS Report

BIOS Information
Vendor: American Megatrends Inc.
Version: M.00
Release Date: 08/05/2019
Address: 0xF0000
Runtime Size: 64 kB
ROM Size: 16 MB
Characteristics:
PCI is supported
BIOS is upgradeable
BIOS shadowing is allowed

System Report

System Information
Manufacturer: Micro-Star International Co., Ltd.
Product Name: MS-7B79
Version: 4.0
Serial Number: To be filled by O.E.M.
UUID: 1c6ca8dc-4684-6c19-ae25-00d861a6e291
Wake-up Type: Power Switch
SKU Number: To be filled by O.E.M.
Family: To be filled by O.E.M.

Base Board Report

Base Board Information
Manufacturer: Micro-Star International Co., Ltd.
Product Name: X470 GAMING PRO MAX (MS-7B79)
Version: 4.0
Serial Number: J816765137
Asset Tag: To be filled by O.E.M.
Features:
Board is a hosting board
Board is replaceable

Cache Report

Cache Information
Socket Designation: L1 - Cache
Configuration: Enabled, Not Socketed, Level 1

Processor Report

Processor Information
Socket Designation: AM4
Type: Central Processor
Family: Zen

Memory Report

Physical Memory Array
Location: System Board Or Motherboard
Use: System Memory

PCI Devices
Slot:	00:00.0
Class:	Host bridge
Vendor:	Advanced Micro Devices, Inc. [AMD]

Network Cards
  *-network
       description: Ethernet interface
       product: RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller
       vendor: Realtek Semiconductor Co., Ltd.
       

SCSI Devices
[2:0:0:0]    disk    ATA      Samsung SSD 840  BB6Q  /dev/sda 


Block Devices
Note: you may identify whether disk is SDD or HDD by ROTA parameter
1 - HDD, 0 - SSD
NAME ROTA   SIZE TYPE MOUNTPOINT MODEL                   SERIAL
sda     0 931,5G disk            Samsung SSD 840 EVO 1TB S1D9NSAF922105B


```

The main objective of hardware detection script is to ensure vendor`s hardware is properly detected by our OS.

####What does the script do?

It runs specific commands and show information about:
 - Hardware components report: SMBIOS data
   (basic system info, processors, memory, baseboard, cache). This report also stores serial numbers
   (if possible to detect) under parameter “Serial Number:” for corresponding section;
 - Network cards;
 - PCI devices;
 - SCSI Devices;
 - Block Devices (with information HDD/SDD);

Report is stored in: `logs/hw_detection.log`
It contains color highlighting, so better to use `cat` to read it.