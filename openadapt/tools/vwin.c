#include <stdio.h>
#include <windows.h>
#include <tlhelp32.h>

typedef struct {
    char title[256];
    DWORD pid;
    SYSTEMTIME startTime;
    char startDate[20];
} WindowInfo;

int compareStartTime(const void* a, const void* b) {
    WindowInfo* windowA = (WindowInfo*)a;
    WindowInfo* windowB = (WindowInfo*)b;
    FILETIME fileTimeA, fileTimeB;
    SystemTimeToFileTime(&windowA->startTime, &fileTimeA);
    SystemTimeToFileTime(&windowB->startTime, &fileTimeB);
    return CompareFileTime(&fileTimeA, &fileTimeB);
}

int main() {
    printf("%-40s %-10s %-20s %-20s\n", "Title", "PID", "Start Time", "Start Date");
    printf("%-40s %-10s %-20s %-20s\n", "-----", "---", "----------", "----------");
    HWND hwnd = GetTopWindow(NULL);
    int numWindows = 0;
    WindowInfo windows[1024];
    while (hwnd != NULL) {
        char title[256];
        GetWindowText(hwnd, title, sizeof(title));
        if (IsWindowVisible(hwnd) && title[0] != '\0') {
            DWORD pid;
            GetWindowThreadProcessId(hwnd, &pid);
            HANDLE hProcess = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, FALSE, pid);
            if (hProcess != NULL) {
                FILETIME createTime, exitTime, kernelTime, userTime;
                if (GetProcessTimes(hProcess, &createTime, &exitTime, &kernelTime, &userTime)) {
                    SYSTEMTIME utcSt, localSt;
                    FileTimeToSystemTime(&createTime, &utcSt);
                    SystemTimeToTzSpecificLocalTime(NULL, &utcSt, &localSt);
                    WindowInfo window = {0};
                    strncpy(window.title, title, sizeof(window.title) - 1);
                    window.pid = pid;
                    window.startTime = localSt;
                    char startDate[20];
                    sprintf(startDate, "%04d-%02d-%02d", localSt.wYear, localSt.wMonth, localSt.wDay);
                    strncpy(window.startDate, startDate, sizeof(window.startDate) - 1);
                    windows[numWindows++] = window;
                } else {
                    CloseHandle(hProcess);
                }
            }
        }
        hwnd = GetNextWindow(hwnd, GW_HWNDNEXT);
    }
    qsort(windows, numWindows, sizeof(WindowInfo), compareStartTime);
    for (int i = 0; i < numWindows; i++) {
        WindowInfo window = windows[i];
        printf("%-40s %-10d %02d:%02d:%02d %20s\n", window.title, window.pid, window.startTime.wHour, window.startTime.wMinute, window.startTime.wSecond, window.startDate);
    }
    return 0;
}
