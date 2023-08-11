#ifndef IN
#define IN
#endif

#ifndef OUT
#endif
#include <stdio.h>
#include <winternl.h>
#include <psapi.h>
#include <shlwapi.h>
#define STATUS_SUCCESS ((NTSTATUS)0x00000000L)
#define STATUS_INFO_LENGTH_MISMATCH ((NTSTATUS)0xC0000004L)
NTSTATUS NTAPI NtQuerySystemInformation(
    IN SYSTEM_INFORMATION_CLASS SystemInformationClass,
    OUT PVOID SystemInformation, IN ULONG SystemInformationLength,
    OUT PULONG ReturnLength OPTIONAL);
