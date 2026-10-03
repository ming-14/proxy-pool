// 启动器：唯一作用是启动 <本程序目录>\proxy-pool\proxy-pool.exe，随后自身退出。
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>

#include <string>

namespace {

// 取得当前 exe 的完整路径（随长度增长重试，兼容超长路径）。
std::wstring modulePath() {
    std::wstring buf(MAX_PATH, L'\0');
    for (;;) {
        DWORD n = GetModuleFileNameW(nullptr, buf.data(), static_cast<DWORD>(buf.size()));
        if (n == 0) {
            return std::wstring();
        }
        if (n < buf.size()) {
            buf.resize(n);
            return buf;
        }
        buf.resize(buf.size() * 2);
    }
}

std::wstring lastErrorMessage(DWORD code) {
    LPWSTR raw = nullptr;
    const DWORD n = FormatMessageW(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
            FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<LPWSTR>(&raw), 0, nullptr);
    std::wstring text = (n != 0 && raw != nullptr) ? std::wstring(raw, n) : std::wstring();
    if (raw != nullptr) {
        LocalFree(raw);
    }
    while (!text.empty() && (text.back() == L'\r' || text.back() == L'\n')) {
        text.pop_back();
    }
    if (text.empty()) {
        text = L"未知错误";
    }
    return text;
}

void fatal(const std::wstring& detail) {
    MessageBoxW(nullptr, detail.c_str(), L"proxy-pool 启动失败",
                MB_OK | MB_ICONERROR | MB_SYSTEMMODAL);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    const std::wstring self = modulePath();
    if (self.empty()) {
        fatal(L"无法获取自身路径，程序无法启动 proxy-pool。");
        return 1;
    }

    const std::size_t slash = self.find_last_of(L"\\/");
    const std::wstring dir =
        (slash == std::wstring::npos) ? std::wstring(L".") : self.substr(0, slash);
    const std::wstring targetDir = dir + L"\\proxy-pool";
    const std::wstring target = targetDir + L"\\proxy-pool.exe";

    if (GetFileAttributesW(target.c_str()) == INVALID_FILE_ATTRIBUTES) {
        fatal(L"找不到目标程序：" + target);
        return 1;
    }

    std::wstring cmdLine = L"\"" + target + L"\"";
    STARTUPINFOW si = {};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};

    // 工作目录设为目标程序所在目录，保证其内部相对路径解析正确
    const BOOL ok = CreateProcessW(target.c_str(), cmdLine.data(), nullptr, nullptr, FALSE,
                                   0, nullptr, targetDir.c_str(), &si, &pi);
    if (!ok) {
        const DWORD err = GetLastError();
        fatal(L"启动失败：" + target + L"，" + lastErrorMessage(err) + L"（错误码 " +
                  std::to_wstring(err) + L"）");
        return 1;
    }

    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
