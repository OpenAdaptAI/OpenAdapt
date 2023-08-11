#include "qsysinfo.h"

int compareProcessCreationTime(const void* a, const void* b) {
    PSYSTEM_PROCESS_INFORMATION p1 = *(PSYSTEM_PROCESS_INFORMATION*)a;
    PSYSTEM_PROCESS_INFORMATION p2 = *(PSYSTEM_PROCESS_INFORMATION*)b;

    if (p1->CreateTime.QuadPart < p2->CreateTime.QuadPart) {
        return -1;
    } else if (p1->CreateTime.QuadPart > p2->CreateTime.QuadPart) {
        return 1;
    } else {
        return 0;
    }
}

int main() {
    ULONG bufferSize = 0;
    TIME_ZONE_INFORMATION tzInfo;
    GetTimeZoneInformation(&tzInfo);
    NTSTATUS status = NtQuerySystemInformation(SystemProcessInformation, NULL,
                                               0, &bufferSize);
    if (status != STATUS_INFO_LENGTH_MISMATCH) {
        printf("NtQuerySystemInformation failed with error %lx\n", status);
        return 1;
    }

    PSYSTEM_PROCESS_INFORMATION processInfo =
        (PSYSTEM_PROCESS_INFORMATION)malloc(bufferSize);
    if (!processInfo) {
        printf("Failed to allocate memory\n");
        return 1;
    }

    status = NtQuerySystemInformation(SystemProcessInformation, processInfo,
                                      bufferSize, NULL);
    if (status != STATUS_SUCCESS) {
        printf("NtQuerySystemInformation failed with error %lx\n", status);
        free(processInfo);
        return 1;
    }

    // Create an array of pointers to process information structures
    PSYSTEM_PROCESS_INFORMATION* processArray =
        (PSYSTEM_PROCESS_INFORMATION*)malloc(
            sizeof(PSYSTEM_PROCESS_INFORMATION) * bufferSize);
    if (!processArray) {
        printf("Failed to allocate memory\n");
        free(processInfo);
        return 1;
    }

    PSYSTEM_PROCESS_INFORMATION current = processInfo;
    int i = 0;
    while (current->NextEntryOffset != 0) {
        processArray[i++] = current;
        current = (PSYSTEM_PROCESS_INFORMATION)((PUCHAR)current +
                                                current->NextEntryOffset);
    }

    // Sort the array based on process creation time
    qsort(processArray, i, sizeof(PSYSTEM_PROCESS_INFORMATION),
          compareProcessCreationTime);

    for (int j = 0; j < i; j++) {
        current = processArray[j];
        HANDLE hProcess =
            OpenProcess(PROCESS_QUERY_INFORMATION, FALSE,
                        (DWORD)PtrToUlong(current->UniqueProcessId));
        if (hProcess != NULL) {
            FILETIME creationTime, exitTime, kernelTime, userTime;
                if (GetProcessTimes(hProcess, &creationTime, &exitTime,
                                    &kernelTime, &userTime)) {
                SYSTEMTIME stCreation, stLocalCreation;
                if (FileTimeToSystemTime(&creationTime, &stCreation)) {
                    printf("Process ID: %lu\tProcess name: %ls\n",
                           (ULONG_PTR)current->UniqueProcessId,
                           current->ImageName.Buffer ? current->ImageName.Buffer
                                                     : L"");
                    printf(
                        "------------------------------------------------------"
                        "----------\n");
                    printf("Parent process ID: %lu\n",
                           (ULONG_PTR)current->InheritedFromUniqueProcessId);

                    WCHAR sizeBuffer[MAX_PATH];
                    PWSTR sizeString = StrFormatByteSizeW(
                        current->PrivatePageCount, sizeBuffer, MAX_PATH);

                    if (sizeString != NULL) {
                        printf("Private memory usage: %ls\n", sizeString);
                    }

                    SystemTimeToTzSpecificLocalTime(&tzInfo, &stCreation,
                                                    &stLocalCreation);
                    printf("Creation time: %02d:%02d:%02d\n",
                           stLocalCreation.wHour, stLocalCreation.wMinute,
                           stLocalCreation.wSecond);

                    printf("\n");
                }
            }
            CloseHandle(hProcess);
        }
    }
    printf(
        "----------------------------------------------------------------\n");
    free(processArray);
    free(processInfo);
    return 0;
}
