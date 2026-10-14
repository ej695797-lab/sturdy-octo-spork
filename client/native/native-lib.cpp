#include <jni.h>
#include <unistd.h>
#include <link.h>
#include <set>
#include <map>
#include <functional>
#include <fcntl.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <cstdio>
#include <cstdint>
#include <thread>
#include <string>
#include <vector>
#include <sstream>
#include <cstring>
#include <cstdlib>
#include <map>
#include <mutex>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <array>
#include <utility>
#include <unwind.h>
#include <android/log.h>
#include <zlib.h>

// Proper stack unwind (libunwind) for clean caller RVAs - far better than a raw
// stack scan for finding the exact managed method that logged something (e.g.
// the obfuscated credential coroutine behind "Failed to get credentials").
struct UnwindState { uint8_t* base; uint8_t* end; std::string out; int n; };
static _Unwind_Reason_Code UnwindCb(struct _Unwind_Context* ctx, void* arg) {
    auto* st = (UnwindState*)arg;
    uintptr_t pc = _Unwind_GetIP(ctx);
    if (pc && (uint8_t*)pc >= st->base && (uint8_t*)pc < st->end) {
        char b[24]; snprintf(b, sizeof(b), " %llx", (unsigned long long)((uint8_t*)pc - st->base));
        st->out += b;
    }
    if (++st->n > 40) return _URC_END_OF_STACK;
    return _URC_NO_REASON;
}
static std::string UnwindRVAs(uint8_t* base) {
    UnwindState st{ base, base ? base + 0x20000000 : nullptr, "", 0 };
    if (base) _Unwind_Backtrace(UnwindCb, &st);
    return st.out;
}

// ---- crash recorder (131b9) -------------------------------------------
// Release builds log nothing useful and tombstones need root, so a native
// crash left no trace. On SIGSEGV/SIGBUS/SIGABRT/SIGILL write the signal,
// fault address, PC + LR (library+offset) and an unwind to the checkpoint
// file, then chain to the previous handler (Unity / debuggerd) unchanged.
#include <signal.h>
#include <sys/ucontext.h>
static struct sigaction g_oldSig[32];
static int g_crashFd = -1;
static volatile int g_crashDumps = 0;
struct CrashBuf { char b[6144]; size_t len; int n; };
static void CrashPut(CrashBuf* s, const char* fmt, uintptr_t pc) {
    Dl_info di{}; const char* lib = "?"; uintptr_t off = pc;
    if (pc && dladdr((void*)pc, &di) && di.dli_fname) {
        lib = strrchr(di.dli_fname, '/') ? strrchr(di.dli_fname, '/') + 1 : di.dli_fname;
        off = pc - (uintptr_t)di.dli_fbase;
    }
    int w = snprintf(s->b + s->len, sizeof(s->b) - s->len, fmt, lib, (unsigned long long)off);
    if (w > 0 && s->len + (size_t)w < sizeof(s->b)) s->len += (size_t)w;
}
static _Unwind_Reason_Code CrashUnwindCb(struct _Unwind_Context* ctx, void* arg) {
    auto* s = (CrashBuf*)arg;
    uintptr_t pc = _Unwind_GetIP(ctx);
    if (pc) CrashPut(s, "  # %s+0x%llx\n", pc);
    return (++s->n >= 48) ? _URC_END_OF_STACK : _URC_NO_REASON;
}
static void CrashHandler(int sig, siginfo_t* si, void* uc) {
    if (g_crashFd >= 0 && g_crashDumps++ < 4) {
        static CrashBuf s; s.len = 0; s.n = 0;
        s.len = (size_t)snprintf(s.b, sizeof(s.b), "CRASH: signal %d code %d fault_addr %p\n",
                                 sig, si ? si->si_code : 0, si ? si->si_addr : nullptr);
        ucontext_t* u = (ucontext_t*)uc;
        if (u) { CrashPut(&s, "  pc %s+0x%llx\n", (uintptr_t)u->uc_mcontext.pc);
                 CrashPut(&s, "  lr %s+0x%llx\n", (uintptr_t)u->uc_mcontext.regs[30]); }
        _Unwind_Backtrace(CrashUnwindCb, &s);
        write(g_crashFd, s.b, s.len);
        fsync(g_crashFd);
    }
    struct sigaction* old = (sig > 0 && sig < 32) ? &g_oldSig[sig] : nullptr;
    if (old && (old->sa_flags & SA_SIGINFO) && old->sa_sigaction) { old->sa_sigaction(sig, si, uc); return; }
    if (old && old->sa_handler != SIG_DFL && old->sa_handler != SIG_IGN && old->sa_handler) { old->sa_handler(sig); return; }
    signal(sig, SIG_DFL); raise(sig);
}
static void InstallCrashRecorder() {
    g_crashFd = open("/sdcard/Android/data/com.TrassGames.Yeeps/files/bnm_checkpoint.txt", O_WRONLY | O_APPEND | O_CREAT, 0644);
    struct sigaction sa{}; sa.sa_sigaction = CrashHandler; sa.sa_flags = SA_SIGINFO | SA_ONSTACK; sigemptyset(&sa.sa_mask);
    for (int sig : {SIGSEGV, SIGBUS, SIGABRT, SIGILL}) sigaction(sig, &sa, &g_oldSig[sig]);
}
#include "extern/Dobby/include/dobby.h"
#include "Include/BNMResolve.hpp"
#include "extern/BNM-Android/src/private/Internals.hpp"
#include "Include/httplib.h"
#include "explorer/html_page.hpp"
#include "explorer/luabnm.hpp"

#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, "BNMExplorer", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "BNMExplorer", __VA_ARGS__)

using namespace BNM;
using namespace BNM::IL2CPP;
using namespace BNM::Structures::Unity;

static int g_port = 8080;

static double nowSeconds() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

static void AttachThread() {
    BNM::AttachIl2Cpp();
}

static std::string jsEsc(const char* s) {
    if (!s) return "";
    std::string o;
    for (const char* p = s; *p; ++p) {
        unsigned char c = (unsigned char)*p;
        if      (c == '"')  o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c == '\n') o += "\\n";
        else if (c == '\r') o += "\\r";
        else if (c == '\t') o += "\\t";
        else if (c < 0x20) { char buf[8]; snprintf(buf, sizeof(buf), "\\u%04x", c); o += buf; }
        else                 o += *p;
    }
    return o;
}

static std::string jsEsc(const std::string& s) { return jsEsc(s.c_str()); }

static std::string typeName(const Il2CppType* t) {
    if (!t) return "?";
    Class c(t);
    if (!c.IsValid()) return "?";
    auto* r = c.GetClass();
    if (!r || !r->name) return "?";
    std::string n = r->name;
    if (r->namespaze && strlen(r->namespaze) > 0)
        n = std::string(r->namespaze) + "." + n;
    return n;
}

struct LoopEntry {
    std::string addr;
    std::string asm_;
    std::string ns;
    std::string cls;
    std::string name;
    std::string ftype;
    bool isProp;
    std::string val;
    int intervalMs;
    std::atomic<bool> active{true};
};

static std::mutex g_loopMtx;
static std::map<std::string, std::shared_ptr<LoopEntry>> g_loops;

struct LogEntry {
    std::string msg;
    int level;
    double timestamp;
};
static std::mutex g_logMtx;
static std::vector<LogEntry> g_logBuffer;
static const size_t LOG_MAX = 500;
static std::atomic<bool> g_logHooked{false};

static void addLogEntry(const std::string& msg, int level) {
    std::lock_guard<std::mutex> lk(g_logMtx);
    if (g_logBuffer.size() >= LOG_MAX) g_logBuffer.erase(g_logBuffer.begin());
    g_logBuffer.push_back({msg, level, nowSeconds()});
}

static void hookDebugLog() {
    if (g_logHooked.exchange(true)) return;
    try {
        Class debugCls("UnityEngine", "Debug");
        if (!debugCls.IsValid()) return;
    } catch(...) {}
}

struct CatcherArg {
    std::string type;
    std::string val;
};

struct CatcherCall {
    double ts;
    std::string instance;
    std::vector<CatcherArg> args;
    std::string ret;
};

struct CatcherEntry {
    std::string id;
    std::string asm_;
    std::string ns;
    std::string cls;
    std::string method;
    std::string retType;
    std::vector<std::string> paramTypes;
    std::vector<std::string> paramNames;
    std::atomic<bool> active{true};
    std::mutex callsMtx;
    std::vector<CatcherCall> calls;
    void* origPtr = nullptr;
    int slotIdx = -1;
    static const size_t MAX_CALLS = 100;
};

static std::mutex g_catcherMtx;
static std::map<std::string, std::shared_ptr<CatcherEntry>> g_catchers;

static std::string logsJson() {
    std::lock_guard<std::mutex> lk(g_logMtx);
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& e : g_logBuffer) {
        if (!first) j << ","; first = false;
        j << "{\"msg\":\"" << jsEsc(e.msg) << "\",\"level\":" << e.level << ",\"t\":" << std::fixed << e.timestamp << "}";
    }
    j << "]";
    return j.str();
}

static std::string jsonVal(const std::string& json, const std::string& key) {
    std::string qk = "\"" + key + "\":";
    auto pos = json.find(qk);
    if (pos == std::string::npos) return "";
    pos += qk.length();
    while (pos < json.length() && (json[pos] == ' ' || json[pos] == '\t')) pos++;
    if (pos < json.length() && json[pos] == '"') {
        pos++;
        size_t end = pos;
        bool esc = false;
        while (end < json.length()) {
            if (json[end] == '\\' && !esc) esc = true;
            else if (json[end] == '"' && !esc) break;
            else esc = false;
            end++;
        }
        if (end >= json.length()) return "";
        std::string raw = json.substr(pos, end - pos);
        std::string out;
        for (size_t i = 0; i < raw.length(); i++) {
            if (raw[i] == '\\' && i + 1 < raw.length()) {
                char n = raw[i+1];
                if      (n == '"')  { out += '"';  i++; }
                else if (n == '\\') { out += '\\'; i++; }
                else if (n == 'n')  { out += '\n'; i++; }
                else if (n == 'r')  { out += '\r'; i++; }
                else out += raw[i];
            } else out += raw[i];
        }
        return out;
    }
    auto end = json.find_first_of(",}", pos);
    if (end == std::string::npos) return "";
    std::string v = json.substr(pos, end - pos);
    while (!v.empty() && (v.back() == ' ' || v.back() == '\t' || v.back() == '\r' || v.back() == '\n')) v.pop_back();
    return v;
}

static MethodBase findObjsMethod() {
    static MethodBase m;
    if (m.IsValid()) return m;
    Class oc("UnityEngine", "Object");
    for (auto& mt : oc.GetMethods(false)) {
        auto* mi = mt.GetInfo();
        if (mi && mi->name && strcmp(mi->name, "FindObjectsOfType") == 0 && mi->parameters_count == 1)
            if (mi->parameters[0] && strstr(typeName(mi->parameters[0]).c_str(), "Type"))
            { m = mt; break; }
    }
    return m;
}

static Il2CppObject* sysTypeOf(const std::string& aqn) {
    Class tc("System", "Type", Image("mscorlib.dll"));
    if (!tc.IsValid()) return nullptr;
    Method<Il2CppObject*> gt(tc.GetMethod("GetType", 1));
    return gt.IsValid() ? gt(CreateMonoString(aqn.c_str())) : nullptr;
}

static bool isObjectAlive(Il2CppObject* obj) {
    if (!obj) return false;
    try {
        Method<bool> m(Class("UnityEngine", "Object").GetMethod("op_Implicit", 1));
        if (m.IsValid()) return m(obj);
        auto* k = obj->klass;
        if (!k || !k->name) return false;
        return true;
    } catch(...) { return false; }
}

static Il2CppObject* resolveComponentByAddr(uintptr_t addr) {
    if (!addr) return nullptr;
    Il2CppObject* obj = (Il2CppObject*)addr;
    if (!isObjectAlive(obj)) return nullptr;
    return obj;
}

static std::string assembliesJson() {
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& img : Image::GetImages()) {
        auto* d = img.GetInfo();
        if (!img.IsValid() || !d || !d->name) continue;
        if (!first) j << ",";
        first = false;
        j << "\"" << jsEsc(d->name) << "\"";
    }
    j << "]";
    return j.str();
}

static std::string classesJson(const std::string& asm_) {
    Image img(asm_);
    if (!img.IsValid()) return "[]";
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& cls : img.GetClasses(false)) {
        auto* r = cls.GetClass();
        if (!cls.IsValid() || !r || !r->name) continue;
        if (!first) j << ",";
        first = false;
        std::string t = "class";
        if (r->enumtype) t = "enum";
        else if (r->byval_arg.valuetype) t = "struct";
        else if (r->flags & 0x20) t = "interface";
        j << "{\"name\":\"" << jsEsc(r->name) << "\",\"ns\":\"" << jsEsc(r->namespaze ? r->namespaze : "") << "\",\"t\":\"" << t << "\"}";
    }
    j << "]";
    return j.str();
}

static std::string allClassesJson() {
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& img : Image::GetImages()) {
        auto* d = img.GetInfo();
        if (!img.IsValid() || !d || !d->name) continue;
        std::string a = d->name;
        for (auto& cls : img.GetClasses(false)) {
            auto* r = cls.GetClass();
            if (!cls.IsValid() || !r || !r->name) continue;
            if (!first) j << ",";
            first = false;
            std::string t = "class";
            if (r->enumtype) t = "enum";
            else if (r->byval_arg.valuetype) t = "struct";
            else if (r->flags & 0x20) t = "interface";
            j << "{\"name\":\"" << jsEsc(r->name) << "\",\"ns\":\"" << jsEsc(r->namespaze ? r->namespaze : "") << "\",\"t\":\"" << t << "\",\"a\":\"" << jsEsc(a) << "\"}";
        }
    }
    j << "]";
    return j.str();
}

static std::string classDetailJson(const std::string& a, const std::string& ns, const std::string& cn) {
    BNM_FileCheckpointF("classDetailJson: start a=%s ns=%s cn=%s", a.c_str(), ns.c_str(), cn.c_str());
    Class cls(ns.c_str(), cn.c_str(), Image(a.c_str()));
    BNM_FileCheckpoint("classDetailJson: Class constructed");
    if (!cls.IsValid()) { BNM_FileCheckpoint("classDetailJson: cls not valid, returning"); return "{}"; }
    BNM_FileCheckpoint("classDetailJson: cls valid, calling GetClass()");
    auto* r = cls.GetClass();
    BNM_FileCheckpointF("classDetailJson: GetClass() returned %p", (void*)r);
    if (!r) return "{}";

    std::ostringstream j;
    j << "{\"name\":\"" << jsEsc(cn) << "\",\"ns\":\"" << jsEsc(ns) << "\",\"asm\":\"" << jsEsc(a) << "\",";
    j << (r->parent && r->parent->name ? "\"parent\":\"" + jsEsc(r->parent->name) + "\"," : "\"parent\":null,");
    BNM_FileCheckpoint("classDetailJson: wrote header, calling GetFields()");

    j << "\"fields\":[";
    bool first = true;
    auto fields = cls.GetFields(false);
    BNM_FileCheckpointF("classDetailJson: GetFields() returned %zu fields", fields.size());
    for (auto& f : fields) {
        auto* fi = f.GetInfo();
        if (!f.IsValid() || !fi || !fi->name) continue;
        if (!first) j << ",";
        first = false;
        j << "{\"name\":\"" << jsEsc(fi->name) << "\",\"type\":\"" << jsEsc(typeName(fi->type)) << "\",\"s\":" << (fi->type && (fi->type->attrs & 0x10) ? "true" : "false") << ",\"off\":" << fi->offset << "}";
    }
    BNM_FileCheckpoint("classDetailJson: fields done, calling GetMethods()");

    j << "],\"methods\":[";
    first = true;
    auto methods = cls.GetMethods(false);
    BNM_FileCheckpointF("classDetailJson: GetMethods() returned %zu methods", methods.size());
    for (auto& m : methods) {
        auto* mi = m.GetInfo();
        if (!m.IsValid() || !mi || !mi->name) continue;
        if (!first) j << ",";
        first = false;
        char ab[32] = {};
        if (mi->methodPointer) snprintf(ab, sizeof(ab), "%llX", (unsigned long long)(uintptr_t)mi->methodPointer);
        j << "{\"name\":\"" << jsEsc(mi->name) << "\",\"ret\":\"" << jsEsc(typeName(mi->return_type)) << "\",\"s\":" << ((mi->flags & 0x10) ? "true" : "false") << ",\"addr\":\"" << ab << "\",\"params\":[";
        for (int i = 0; i < (int)mi->parameters_count; i++) {
            if (i > 0) j << ",";
            j << "{\"n\":\"arg" << i << "\",\"t\":\"" << jsEsc(mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?") << "\"}";
        }
        j << "]}";
    }

    j << "],\"props\":[";
    first = true;
    for (auto& p : cls.GetProperties(false)) {
        auto* pi = p._data;
        if (!p.IsValid() || !pi || !pi->name) continue;
        if (!first) j << ",";
        first = false;
        j << "{\"name\":\"" << jsEsc(pi->name) << "\",\"type\":\"" << jsEsc(pi->get && pi->get->return_type ? typeName(pi->get->return_type) : "?") << "\",\"g\":" << (pi->get ? "true" : "false") << ",\"s\":" << (pi->set ? "true" : "false") << "}";
    }
    j << "]}";
    return j.str();
}

struct InvokeResult { bool ok; std::string val, err; };

static InvokeResult invokeMethod(const std::string& body) {
    AttachThread();
    InvokeResult res = {false, "", ""};
    std::string a = jsonVal(body, "asm"), ns = jsonVal(body, "ns"), cn = jsonVal(body, "cls"), mn = jsonVal(body, "method");
    bool isStatic = jsonVal(body, "static") == "true" || jsonVal(body, "static") == "1";
    uintptr_t instAddr = (uintptr_t)strtoull(jsonVal(body, "instance").c_str(), nullptr, 16);

    std::vector<std::string> argT, argV;
    auto ap = body.find("\"args\":[");
    if (ap != std::string::npos) {
        ap += 8;
        auto ae = body.find("]", ap);
        if (ae != std::string::npos) {
            std::string ab = body.substr(ap, ae - ap);
            size_t p = 0;
            while (p < ab.size()) {
                auto ob = ab.find("{", p), cb = ab.find("}", ob);
                if (ob == std::string::npos || cb == std::string::npos) break;
                std::string e = "{" + ab.substr(ob + 1, cb - ob - 1) + "}";
                argT.push_back(jsonVal(e, "t"));
                argV.push_back(jsonVal(e, "v"));
                p = cb + 1;
            }
        }
    }

    if (a.empty() || cn.empty() || mn.empty()) { res.err = "Missing asm/cls/method"; return res; }

    Class cls(ns.c_str(), cn.c_str(), Image(a.c_str()));
    if (!cls.IsValid()) { res.err = "Class not found"; return res; }

    MethodBase mb = cls.GetMethod(mn.c_str(), (int)argT.size());
    if (!mb.IsValid()) mb = cls.GetMethod(mn.c_str());
    if (!mb.IsValid()) {
        for (Class cur = cls.GetParent(); cur.IsValid() && cur.GetClass() && !mb.IsValid(); cur = cur.GetParent()) {
            mb = cur.GetMethod(mn.c_str(), (int)argT.size());
            if (!mb.IsValid()) mb = cur.GetMethod(mn.c_str());
        }
    }
    if (!mb.IsValid()) { res.err = "Method not found"; return res; }
    auto* mi = mb.GetInfo();
    if (!mi || !mi->methodPointer) { res.err = "No pointer"; return res; }

    std::vector<void*> runtimeArgs;
    std::vector<int32_t> vi;
    std::vector<int64_t> vi64;
    std::vector<float> vf;
    std::vector<double> vd;
    std::vector<uint8_t> vb;
    std::vector<int16_t> vs16;
    std::vector<Vector3> vv3;
    std::vector<Vector2> vv2;
    std::vector<Color> vc;
    std::vector<Vector4> vv4;

    for (size_t i = 0; i < argT.size(); i++) {
        auto& t = argT[i]; auto& v = argV[i];
        if (t=="System.Int32"||t=="Int32"||t=="int") {
            vi.push_back(std::stoi(v.empty()?"0":v)); runtimeArgs.push_back(&vi.back());
        } else if (t=="System.Int64"||t=="Int64"||t=="long") {
            vi64.push_back(std::stoll(v.empty()?"0":v)); runtimeArgs.push_back(&vi64.back());
        } else if (t=="System.Single"||t=="Single"||t=="float") {
            vf.push_back(std::stof(v.empty()?"0":v)); runtimeArgs.push_back(&vf.back());
        } else if (t=="System.Double"||t=="Double"||t=="double") {
            vd.push_back(std::stod(v.empty()?"0":v)); runtimeArgs.push_back(&vd.back());
        } else if (t=="System.Boolean"||t=="Boolean"||t=="bool") {
            vb.push_back((uint8_t)(v=="true"||v=="1"?1:0)); runtimeArgs.push_back(&vb.back());
        } else if (t=="System.Byte"||t=="Byte"||t=="byte") {
            vb.push_back((uint8_t)std::stoul(v.empty()?"0":v)); runtimeArgs.push_back(&vb.back());
        } else if (t=="System.Int16"||t=="Int16"||t=="short") {
            vs16.push_back((int16_t)std::stoi(v.empty()?"0":v)); runtimeArgs.push_back(&vs16.back());
        } else if (t=="UnityEngine.Vector3"||t=="Vector3") {
            float x=0,y=0,z=0; sscanf(v.c_str(),"[%f,%f,%f]",&x,&y,&z); vv3.push_back({x,y,z}); runtimeArgs.push_back(&vv3.back());
        } else if (t=="UnityEngine.Vector2"||t=="Vector2") {
            float x=0,y=0; sscanf(v.c_str(),"[%f,%f]",&x,&y); vv2.push_back({x,y}); runtimeArgs.push_back(&vv2.back());
        } else if (t=="UnityEngine.Color"||t=="Color") {
            float r=1,g=1,b=1,aa=1; sscanf(v.c_str(),"[%f,%f,%f,%f]",&r,&g,&b,&aa); vc.push_back({r,g,b,aa}); runtimeArgs.push_back(&vc.back());
        } else if (t=="UnityEngine.Vector4"||t=="Vector4"||t=="UnityEngine.Quaternion"||t=="Quaternion") {
            float x=0,y=0,z=0,w=0; sscanf(v.c_str(),"[%f,%f,%f,%f]",&x,&y,&z,&w); vv4.push_back({x,y,z,w}); runtimeArgs.push_back(&vv4.back());
        } else if (t=="System.String"||t=="String"||t=="string") {
            runtimeArgs.push_back(CreateMonoString(v));
        } else {
            runtimeArgs.push_back((void*)strtoull(v.empty()?"0":v.c_str(), nullptr, 16));
        }
    }

    void* inst = isStatic ? nullptr : (void*)instAddr;
    std::string rtn = typeName(mi->return_type);
    std::ostringstream out;

    Il2CppException* exc = nullptr;
    void* ret = BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(mi, inst, runtimeArgs.empty() ? nullptr : runtimeArgs.data(), &exc);

    if (exc) {
        BNM::Structures::Mono::String* excMsg = nullptr;
        try {
            auto excCls = Class(((Il2CppObject*)exc)->klass);
            if (excCls.IsValid()) {
                auto msgMethod = excCls.GetMethod("get_Message", 0);
                if (msgMethod.IsValid()) {
                    auto* msgMi = msgMethod.GetInfo();
                    if (msgMi && msgMi->methodPointer) {
                        Il2CppException* inner = nullptr;
                        auto* msgRet = (Il2CppObject*)BNM::Internal::il2cppMethods.il2cpp_runtime_invoke(msgMi, exc, nullptr, &inner);
                        if (msgRet && !inner) excMsg = (BNM::Structures::Mono::String*)msgRet;
                    }
                }
            }
        } catch(...) {}
        res.err = excMsg ? excMsg->str() : "IL2CPP exception";
        return res;
    }

    if (rtn=="System.Void"||rtn=="Void"||rtn=="void"||rtn=="?") {
        out << "void";
    } else if (rtn=="System.Single"||rtn=="Single"||rtn=="float") {
        float v = ret ? *(float*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Double"||rtn=="Double"||rtn=="double") {
        double v = ret ? *(double*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Int32"||rtn=="Int32"||rtn=="int") {
        int v = ret ? *(int*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Int64"||rtn=="Int64"||rtn=="long") {
        int64_t v = ret ? *(int64_t*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << v;
    } else if (rtn=="System.Boolean"||rtn=="Boolean"||rtn=="bool") {
        bool v = ret ? *(bool*)((uint8_t*)ret + sizeof(Il2CppObject)) : false;
        out << (v?"true":"false");
    } else if (rtn=="System.Byte"||rtn=="Byte"||rtn=="byte") {
        uint8_t v = ret ? *(uint8_t*)((uint8_t*)ret + sizeof(Il2CppObject)) : 0;
        out << (int)v;
    } else if (rtn=="UnityEngine.Vector3"||rtn=="Vector3") {
        Vector3 v = ret ? *(Vector3*)((uint8_t*)ret + sizeof(Il2CppObject)) : Vector3{0,0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f, %.4f]",v.x,v.y,v.z); out<<buf;
    } else if (rtn=="UnityEngine.Vector2"||rtn=="Vector2") {
        Vector2 v = ret ? *(Vector2*)((uint8_t*)ret + sizeof(Il2CppObject)) : Vector2{0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f]",v.x,v.y); out<<buf;
    } else if (rtn=="UnityEngine.Color"||rtn=="Color") {
        Color v = ret ? *(Color*)((uint8_t*)ret + sizeof(Il2CppObject)) : Color{0,0,0,0};
        char buf[128]; snprintf(buf,sizeof(buf),"[%.4f, %.4f, %.4f, %.4f]",v.r,v.g,v.b,v.a); out<<buf;
    } else if (rtn=="System.String"||rtn=="String"||rtn=="string") {
        auto* s = (BNM::Structures::Mono::String*)ret;
        out << (s ? s->str() : "null");
    } else {
        char buf[64]; snprintf(buf,sizeof(buf),"0x%llX",(unsigned long long)(uintptr_t)ret);
        out << rtn << " @ " << buf;
    }
    res.ok = true; res.val = out.str();
    return res;
}

static std::string instancesJson(const std::string& a, const std::string& ns, const std::string& cn) {
    AttachThread();
    std::string full = ns.empty() ? cn : ns + "." + cn;
    std::string aNoExt = a;
    auto dp = aNoExt.find(".dll");
    if (dp != std::string::npos) aNoExt = aNoExt.substr(0, dp);

    Il2CppObject* st = sysTypeOf(full + ", " + aNoExt);
    if (!st) st = sysTypeOf(full);
    if (!st) return "{\"error\":\"Class not found / System.Type missing\"}";

    auto fm = findObjsMethod();
    if (!fm.IsValid()) return "{\"error\":\"FindObjectsOfType missing\"}";

    std::ostringstream j;
    j << "{\"instances\":[";
    try {
        auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
        if (arr && arr->capacity > 0) {
            bool first = true;
            Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
            for (int i = 0; i < arr->capacity; i++) {
                Il2CppObject* obj = arr->m_Items[i];
                if (!obj) continue;
                if (!first) j << ",";
                first = false;
                std::string name = "obj";
                if (gn.IsValid()) { auto* s = gn[obj](); if (s) name = s->str(); }
                char addr[32]; snprintf(addr, sizeof(addr), "%llX", (unsigned long long)(uintptr_t)obj);
                j << "{\"addr\":\"" << addr << "\",\"name\":\"" << jsEsc(name) << "\"}";
            }
        }
    } catch(...) {}
    j << "]}";
    return j.str();
}

static std::string sceneJson() {
    AttachThread();
    auto fm = findObjsMethod();
    if (!fm.IsValid()) return "[]";

    Il2CppObject* st = sysTypeOf("UnityEngine.GameObject, UnityEngine.CoreModule");
    if (!st) st = sysTypeOf("UnityEngine.GameObject, UnityEngine");
    if (!st) st = sysTypeOf("UnityEngine.GameObject");
    if (!st) return "[]";

    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    Method<bool> ga(Class("UnityEngine","GameObject").GetMethod("get_activeSelf"));
    Method<Il2CppObject*> gt(Class("UnityEngine","GameObject").GetMethod("get_transform"));
    Method<Il2CppObject*> tp(Class("UnityEngine","Transform").GetMethod("get_parent"));
    Method<Il2CppObject*> cg(Class("UnityEngine","Component").GetMethod("get_gameObject"));

    std::ostringstream j;
    j << "[";
    try {
        auto* arr = Method<Array<Il2CppObject*>*>(fm)(st);
        if (arr && arr->capacity > 0) {
            bool first = true;
            for (int i = 0; i < arr->capacity; i++) {
                Il2CppObject* go = arr->m_Items[i];
                if (!go) continue;
                std::string name = "Unknown";
                bool active = false;
                if (gn.IsValid()) { auto* s = gn[go](); if (s) name = s->str(); }
                if (ga.IsValid()) active = ga[go]();
                uintptr_t par = 0;
                if (gt.IsValid() && tp.IsValid() && cg.IsValid()) {
                    Il2CppObject* tr = gt[go]();
                    if (tr) { Il2CppObject* pt = tp[tr](); if (pt) { Il2CppObject* pg = cg[pt](); if (pg) par = (uintptr_t)pg; } }
                }
                if (!first) j << ",";
                first = false;
                j << "{\"addr\":\"" << std::hex << (uintptr_t)go << "\",\"name\":\"" << jsEsc(name) << "\",\"active\":" << (active?"true":"false") << ",\"parent\":\"" << std::hex << par << "\"}";
            }
        }
    } catch(...) {}
    j << "]";
    return j.str();
}

static std::string readField(const std::string& ft, Class& cls, Il2CppObject* inst, const std::string& name, MethodBase* getter, bool isStatic, bool& ok) {
    ok = false;
    if (!inst && !isStatic) return "";
    try {
        if (ft=="System.Single"||ft=="Single"||ft=="float") {
            float v = 0;
            if (getter) { Method<float> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<float> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
            double v = 0;
            if (getter) { Method<double> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<double> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
            int v = 0;
            if (getter) { Method<int> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt32"||ft=="UInt32"||ft=="uint") {
            uint32_t v = 0;
            if (getter) { Method<uint32_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint32_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int64"||ft=="Int64"||ft=="long") {
            int64_t v = 0;
            if (getter) { Method<int64_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt64"||ft=="UInt64"||ft=="ulong") {
            uint64_t v = 0;
            if (getter) { Method<uint64_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Int16"||ft=="Int16"||ft=="short") {
            int16_t v = 0;
            if (getter) { Method<int16_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.UInt16"||ft=="UInt16"||ft=="ushort") {
            uint16_t v = 0;
            if (getter) { Method<uint16_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Byte"||ft=="Byte"||ft=="byte") {
            uint8_t v = 0;
            if (getter) { Method<uint8_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<uint8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.SByte"||ft=="SByte"||ft=="sbyte") {
            int8_t v = 0;
            if (getter) { Method<int8_t> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
            bool v = false;
            if (getter) { Method<bool> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<bool> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return v ? "true" : "false";
        } else if (ft=="System.String"||ft=="String"||ft=="string") {
            BNM::Structures::Mono::String* s = nullptr;
            if (getter) { Method<BNM::Structures::Mono::String*> m(*getter); if(!isStatic) m.SetInstance(inst); s = m(); }
            else { Field<BNM::Structures::Mono::String*> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);s=f();} }
            ok = true; return s ? "\"" + jsEsc(s->str()) + "\"" : "\"\"";
        } else if (ft=="UnityEngine.Vector3"||ft=="Vector3") {
            Vector3 v = {0,0,0};
            if (getter) { Method<Vector3> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector3> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return "[" + std::to_string(v.x) + "," + std::to_string(v.y) + "," + std::to_string(v.z) + "]";
        } else if (ft=="UnityEngine.Color"||ft=="Color") {
            Color c = {0,0,0,0};
            if (getter) { Method<Color> m(*getter); if(!isStatic) m.SetInstance(inst); c = m(); }
            else { Field<Color> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);c=f();} }
            ok = true; return "[" + std::to_string(c.r) + "," + std::to_string(c.g) + "," + std::to_string(c.b) + "," + std::to_string(c.a) + "]";
        } else if (ft=="UnityEngine.Vector2"||ft=="Vector2") {
            Vector2 v = {0,0};
            if (getter) { Method<Vector2> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector2> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return "[" + std::to_string(v.x) + "," + std::to_string(v.y) + "]";
        } else if (ft=="UnityEngine.Vector4"||ft=="Vector4"||ft=="UnityEngine.Quaternion"||ft=="Quaternion") {
            Vector4 v = {0,0,0,0};
            if (getter) { Method<Vector4> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Vector4> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::string("{\"x\":") + std::to_string(v.x) + ",\"y\":" + std::to_string(v.y) + ",\"z\":" + std::to_string(v.z) + ",\"w\":" + std::to_string(v.w) + "}";
        } else if (ft=="UnityEngine.LayerMask"||ft=="LayerMask") {
            int v = 0;
            if (getter) { Method<int> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true; return std::to_string(v);
        } else if (ft=="UnityEngine.Rect"||ft=="Rect") {
            Rect v = {0,0,0,0};
            if (getter) { Method<Rect> m(*getter); if(!isStatic) m.SetInstance(inst); v = m(); }
            else { Field<Rect> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);v=f();} }
            ok = true;
            return std::string("{\"x\":") + std::to_string(v.x) + ",\"y\":" + std::to_string(v.y) + ",\"w\":" + std::to_string(v.w) + ",\"h\":" + std::to_string(v.h) + "}";
        }
    } catch(...) {}
    return "";
}

static void writeField(const std::string& ft, Class& cls, Il2CppObject* inst, const std::string& name, MethodBase* setter, const std::string& v, bool isStatic = false) {
    if (!inst && !isStatic) return;
    try {
        if (ft=="System.Single"||ft=="Single"||ft=="float") {
            float val = std::stof(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<float> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Double"||ft=="Double"||ft=="double") {
            double val = std::stod(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<double> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int32"||ft=="Int32"||ft=="int") {
            int val = std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt32"||ft=="UInt32"||ft=="uint") {
            uint32_t val = (uint32_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint32_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int64"||ft=="Int64"||ft=="long") {
            int64_t val = std::stoll(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt64"||ft=="UInt64"||ft=="ulong") {
            uint64_t val = std::stoull(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint64_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Int16"||ft=="Int16"||ft=="short") {
            int16_t val = (int16_t)std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.UInt16"||ft=="UInt16"||ft=="ushort") {
            uint16_t val = (uint16_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint16_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Byte"||ft=="Byte"||ft=="byte") {
            uint8_t val = (uint8_t)std::stoul(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<uint8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.SByte"||ft=="SByte"||ft=="sbyte") {
            int8_t val = (int8_t)std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int8_t> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.Boolean"||ft=="Boolean"||ft=="bool") {
            bool val = v=="true"||v=="1";
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<bool> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="System.String"||ft=="String"||ft=="string") {
            auto* s = CreateMonoString(v.c_str());
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(s); }
            else { Field<BNM::Structures::Mono::String*> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=s;} }
        } else if (ft=="UnityEngine.Vector3"||ft=="Vector3") {
            float x=0,y=0,z=0; sscanf(v.c_str(),"[%f,%f,%f]",&x,&y,&z); Vector3 val={x,y,z};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector3> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Color"||ft=="Color") {
            float r=1,g=1,b=1,a=1; sscanf(v.c_str(),"[%f,%f,%f,%f]",&r,&g,&b,&a); Color val={r,g,b,a};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Color> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Vector2"||ft=="Vector2") {
            float x=0,y=0; sscanf(v.c_str(),"[%f,%f]",&x,&y); Vector2 val={x,y};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector2> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Vector4"||ft=="Vector4"||ft=="UnityEngine.Quaternion"||ft=="Quaternion") {
            float x=0,y=0,z=0,w=0;
            sscanf(v.c_str(),"{\"x\":%f,\"y\":%f,\"z\":%f,\"w\":%f}",&x,&y,&z,&w);
            Vector4 val={x,y,z,w};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Vector4> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.LayerMask"||ft=="LayerMask") {
            int val = std::stoi(v);
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<int> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        } else if (ft=="UnityEngine.Rect"||ft=="Rect") {
            float x=0,y=0,w=0,h=0;
            sscanf(v.c_str(),"{\"x\":%f,\"y\":%f,\"w\":%f,\"h\":%f}",&x,&y,&w,&h);
            Rect val={x,y,w,h};
            if (setter) { Method<void> m(*setter); if(!isStatic) m.SetInstance(inst); m(val); }
            else { Field<Rect> f=cls.GetField(name.c_str()); if(f.IsValid()){if(!isStatic)f.SetInstance(inst);f=val;} }
        }
    } catch(...) {}
}

static std::string goInfoJson(uintptr_t addr) {
    AttachThread();
    if (!addr) return "{}";
    Il2CppObject* go = (Il2CppObject*)addr;
    if (!isObjectAlive(go)) return "{\"stale\":true}";
    std::string name = "Unknown";
    bool active = false;

    Class goCls("UnityEngine","GameObject");
    Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
    Method<bool> ga(goCls.GetMethod("get_activeSelf"));
    Method<Il2CppObject*> gt(goCls.GetMethod("get_transform"));

    try {
        if (gn.IsValid()) { auto* s=gn[go](); if(s) name=s->str(); }
        if (ga.IsValid()) active = ga[go]();
    } catch(...) {}

    std::ostringstream j;
    j << "{\"addr\":\"" << std::hex << addr << "\",\"name\":\"" << jsEsc(name) << "\",\"active\":" << (active?"true":"false") << ",\"transform\":{";
    try {
        if (gt.IsValid()) {
            Il2CppObject* tr = gt[go]();
            if (tr) {
                Method<Vector3> gp(Class("UnityEngine","Transform").GetMethod("get_localPosition"));
                Method<Vector3> gr(Class("UnityEngine","Transform").GetMethod("get_localEulerAngles"));
                Method<Vector3> gs(Class("UnityEngine","Transform").GetMethod("get_localScale"));
                Vector3 p=gp.IsValid()?gp[tr]():Vector3{0,0,0};
                Vector3 r=gr.IsValid()?gr[tr]():Vector3{0,0,0};
                Vector3 s=gs.IsValid()?gs[tr]():Vector3{0,0,0};
                j << "\"addr\":\"" << std::hex << (uintptr_t)tr << "\",\"p\":[" << p.x << "," << p.y << "," << p.z << "],\"r\":[" << r.x << "," << r.y << "," << r.z << "],\"s\":[" << s.x << "," << s.y << "," << s.z << "]";
            }
        }
    } catch(...) {}
    j << "},\"scripts\":[";

    try {
        Class componentCls("UnityEngine", "Component");
        Il2CppObject* compReflType = nullptr;
        if (componentCls.IsValid()) compReflType = (Il2CppObject*)componentCls.GetMonoType();
        if (!compReflType) compReflType = sysTypeOf("UnityEngine.Component, UnityEngine.CoreModule");
        if (!compReflType) compReflType = sysTypeOf("UnityEngine.Component");
        Array<Il2CppObject*>* comps = nullptr;
        if (!comps && compReflType) {
            for (auto& mt : goCls.GetMethods(true)) {
                auto* mi = mt.GetInfo();
                if (!mi || !mi->name || mi->is_generic) continue;
                if (strcmp(mi->name, "GetComponents") != 0) continue;
                if (mi->parameters_count == 1) {
                    try {
                        Method<Array<Il2CppObject*>*> m(mt);
                        comps = m[go](compReflType);
                    } catch(...) { LOGE("goInfoJson: GetComponents(Type) threw"); }
                    if (comps && comps->capacity > 0) break;
                    comps = nullptr;
                }
            }
        }
        if (!comps && compReflType) {
            for (auto& mt : goCls.GetMethods(true)) {
                auto* mi = mt.GetInfo();
                if (!mi || !mi->name) continue;
                if (strcmp(mi->name, "GetComponentsInternal") != 0) continue;
                LOGD("goInfoJson: found GetComponentsInternal with %d params", mi->parameters_count);
                if (mi->parameters_count >= 2) {
                    try {
                        if (mi->parameters_count == 6) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, true, false, true, false, nullptr);
                            LOGD("goInfoJson: GetComponentsInternal(6) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        } else if (mi->parameters_count == 5) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, true, false, true, false);
                            LOGD("goInfoJson: GetComponentsInternal(5) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        } else if (mi->parameters_count == 4) {
                            Method<Array<Il2CppObject*>*> m(mt);
                            comps = m[go](compReflType, false, true, false);
                            LOGD("goInfoJson: GetComponentsInternal(4) returned %p, count=%d", comps, comps ? (int)comps->capacity : -1);
                        }
                    } catch(...) { LOGE("goInfoJson: GetComponentsInternal threw"); }
                    if (comps && comps->capacity > 0) break;
                    comps = nullptr;
                }
            }
        }
        if (!comps) {
            LOGD("goInfoJson: falling back to FindObjectsOfType");
            Class monoCls("UnityEngine", "MonoBehaviour");
            Il2CppObject* monoType = nullptr;
            if (monoCls.IsValid()) monoType = (Il2CppObject*)monoCls.GetMonoType();
            if (!monoType) monoType = sysTypeOf("UnityEngine.MonoBehaviour, UnityEngine.CoreModule");
            if (!monoType) monoType = sysTypeOf("UnityEngine.MonoBehaviour");

            auto fom = findObjsMethod();
            Method<Il2CppObject*> compGetGO(Class("UnityEngine","Component").GetMethod("get_gameObject"));

            if (fom.IsValid() && monoType && compGetGO.IsValid()) {
                auto* allMonos = Method<Array<Il2CppObject*>*>(fom)(monoType);
                LOGD("goInfoJson: FindObjectsOfType(MonoBehaviour) count=%d", allMonos ? (int)allMonos->capacity : -1);
                if (allMonos && allMonos->capacity > 0) {
                    for (int i = 0; i < allMonos->capacity; i++) {
                        Il2CppObject* c = allMonos->m_Items[i];
                        if (!c) continue;
                        try {
                            Il2CppObject* cgo = compGetGO[c]();
                            if ((uintptr_t)cgo != addr) allMonos->m_Items[i] = nullptr;
                        } catch(...) { allMonos->m_Items[i] = nullptr; }
                    }
                    comps = allMonos;
                }
            }
        }

        LOGD("goInfoJson: final comps=%p, count=%d", comps, comps ? (int)comps->capacity : -1);

        static const char* kStopAtComponent[] = {
                "Component", "Object", nullptr
        };
        static const char* kSkipProps[] = {
                "isActiveAndEnabled", "transform", "gameObject", "tag", "name",
                "hideFlags", "rigidbody", "rigidbody2D", "camera", "light",
                "animation", "constantForce", "renderer", "audio", "networkView",
                "collider", "collider2D", "hingeJoint", "particleSystem", nullptr
        };

        auto isStopClass = [](const char* n) -> bool {
            for (auto** s = kStopAtComponent; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };
        auto isSkipProp = [](const char* n) -> bool {
            for (auto** s = kSkipProps; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };

        auto isKnownBuiltinBase = [](const char* n) -> bool {
            static const char* builtins[] = {
                    "MonoBehaviour", "Behaviour",
                    "Collider", "Collider2D",
                    "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider", "TerrainCollider", "WheelCollider",
                    "BoxCollider2D", "CircleCollider2D", "PolygonCollider2D", "EdgeCollider2D", "CapsuleCollider2D", "CompositeCollider2D",
                    "Rigidbody", "Rigidbody2D",
                    "Renderer", "MeshRenderer", "SkinnedMeshRenderer", "SpriteRenderer", "LineRenderer", "TrailRenderer", "ParticleSystemRenderer",
                    "MeshFilter",
                    "Animator", "Animation",
                    "AudioSource", "AudioListener",
                    "Camera", "Light", "LensFlare", "Projector",
                    "Canvas", "CanvasGroup", "CanvasRenderer",
                    "RectTransform",
                    "Text", "Image", "RawImage", "Button", "Toggle", "Slider", "Scrollbar", "Dropdown", "InputField", "ScrollRect", "Mask", "RectMask2D",
                    "TMP_Text", "TextMeshPro", "TextMeshProUGUI",
                    "NavMeshAgent", "NavMeshObstacle",
                    "ParticleSystem",
                    "Joint", "HingeJoint", "FixedJoint", "SpringJoint", "CharacterJoint", "ConfigurableJoint",
                    "Joint2D", "HingeJoint2D", "FixedJoint2D", "SpringJoint2D", "WheelJoint2D", "SliderJoint2D",
                    "CharacterController",
                    "LODGroup",
                    "OcclusionArea", "OcclusionPortal",
                    "NetworkView",
                    "ConstantForce",
                    nullptr
            };
            for (auto** s = builtins; *s; s++)
                if (strcmp(n, *s) == 0) return true;
            return false;
        };

        if (comps && comps->capacity > 0) {
            bool firstScript = true;

            for (int i = 0; i < comps->capacity; i++) {
                Il2CppObject* comp = comps->m_Items[i];
                if (!comp || !comp->klass) continue;

                auto* klass = comp->klass;

                bool isSupported = false;
                std::string compCategory = "script";
                for (auto* cur = klass; cur; cur = cur->parent) {
                    if (!cur->name) continue;
                    if (strcmp(cur->name, "MonoBehaviour") == 0) { isSupported = true; compCategory = "script"; break; }
                    if (isKnownBuiltinBase(cur->name) && strcmp(cur->name, "MonoBehaviour") != 0 && strcmp(cur->name, "Behaviour") != 0) {
                        isSupported = true; compCategory = "builtin"; break;
                    }
                    if (strcmp(cur->name, "Component") == 0 || strcmp(cur->name, "Object") == 0) break;
                }
                if (!isSupported) continue;

                std::string compName = klass->name ? klass->name : "Unknown";
                std::string compNs = klass->namespaze ? klass->namespaze : "";
                char compAddr[32]; snprintf(compAddr, sizeof(compAddr), "%llX", (unsigned long long)(uintptr_t)comp);
                std::string compAsm = (klass->image && klass->image->name) ? klass->image->name : "";

                bool hasEnabledProp = false;
                bool enabled = false;
                for (auto* cur = klass; cur; cur = cur->parent) {
                    if (!cur->name) continue;
                    if (strcmp(cur->name, "Behaviour") == 0 || strcmp(cur->name, "Renderer") == 0 ||
                        strcmp(cur->name, "Collider") == 0 || strcmp(cur->name, "Collider2D") == 0) {
                        hasEnabledProp = true; break;
                    }
                    if (strcmp(cur->name, "Component") == 0) break;
                }
                if (hasEnabledProp) {
                    Method<bool> getEnabled(Class("UnityEngine","Behaviour").GetMethod("get_enabled"));
                    if (!getEnabled.IsValid()) {
                        Class compClsCheck(klass);
                        auto ep = compClsCheck.GetProperty("enabled");
                        if (ep.IsValid() && ep._data && ep._data->get) {
                            MethodBase egetter(ep._data->get);
                            Method<bool> m(egetter); m.SetInstance(comp);
                            try { enabled = m(); } catch(...) {}
                        }
                    } else {
                        try { enabled = getEnabled[comp](); } catch(...) {}
                    }
                }

                if (!firstScript) j << ",";
                firstScript = false;
                j << "{\"addr\":\"" << compAddr << "\",\"name\":\"" << jsEsc(compName) << "\",\"ns\":\"" << jsEsc(compNs) << "\",\"asm\":\"" << jsEsc(compAsm) << "\",\"category\":\"" << compCategory << "\",\"enabled\":" << (enabled?"true":"false") << ",\"fields\":[";

                LOGD("goInfoJson: found component '%s' category='%s' at %s", compName.c_str(), compCategory.c_str(), compAddr);

                try {
                    Class compCls(klass);
                    bool firstF = true;
                    std::vector<std::string> seen;

                    for (Class cur = compCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                        auto* cn_ = cur.GetClass()->name;
                        if (!cn_) break;
                        if (isStopClass(cn_)) break;

                        for (auto& f : cur.GetFields(false)) {
                            try {
                                auto* fi = f.GetInfo();
                                if (!fi || !fi->type || (fi->type->attrs & 0x10)) continue;
                                std::string fn = fi->name;
                                bool dup = false; for (auto& s : seen) if (s==fn) dup=true;
                                if (dup) continue; seen.push_back(fn);
                                std::string ft = typeName(fi->type);
                                bool ok = false;
                                std::string vs = readField(ft, cur, comp, fn, nullptr, false, ok);
                                if (ok) {
                                    if (!firstF) j << ","; firstF = false;
                                    j << "{\"name\":\"" << jsEsc(fn) << "\",\"type\":\"" << jsEsc(ft) << "\",\"val\":" << vs << ",\"isProp\":false,\"canWrite\":true}";
                                }
                            } catch(...) {}
                        }

                        for (auto& p : cur.GetProperties(false)) {
                            try {
                                auto* pi = p._data;
                                if (!pi || !pi->get) continue;
                                std::string pn = pi->name;
                                if (isSkipProp(pn.c_str())) continue;
                                bool dup = false; for (auto& s : seen) if (s==pn) dup=true;
                                if (dup) continue; seen.push_back(pn);
                                std::string pt = typeName(pi->get->return_type);
                                bool ok = false;
                                MethodBase getter(pi->get);
                                std::string vs = readField(pt, cur, comp, pn, &getter, false, ok);
                                if (ok) {
                                    if (!firstF) j << ","; firstF = false;
                                    j << "{\"name\":\"" << jsEsc(pn) << "\",\"type\":\"" << jsEsc(pt) << "\",\"val\":" << vs << ",\"isProp\":true,\"canWrite\":" << (pi->set?"true":"false") << "}";
                                }
                            } catch(...) {}
                        }
                    }
                } catch(...) {}
                j << "]}";
            }
        }
    } catch(...) {
        LOGE("goInfoJson: exception in script enumeration");
    }
    j << "]}";
    return j.str();
}

// GameManager can't be edited from the explorer (131b18, owner 2026-10-11): it's the
// class players used to turn on creative mode and break maps. Everything else stays writable.
static bool isBlockedClass(const std::string& clsName) {
    std::string low;
    low.reserve(clsName.size());
    for (char c : clsName) low.push_back((char)tolower((unsigned char)c));
    return low.find("gamemanager") != std::string::npos;
}

static void applyLoopEntry(const std::shared_ptr<LoopEntry>& e) {
    if (isBlockedClass(e->cls)) return;   // GameManager is locked (131b18)
    try {
        uintptr_t addr = (uintptr_t)strtoull(e->addr.c_str(), nullptr, 16);
        if (!addr) return;
        Il2CppObject* obj = resolveComponentByAddr(addr);
        if (!obj) { e->active = false; return; }
        for (Class cur(e->ns.c_str(), e->cls.c_str(), Image(e->asm_.c_str())); cur.IsValid(); cur = cur.GetParent()) {
            if (e->isProp) {
                auto p = cur.GetProperty(e->name.c_str());
                if (p.IsValid() && p._data && p._data->set) {
                    MethodBase s(p._data->set);
                    writeField(e->ftype, cur, obj, e->name, &s, e->val);
                    return;
                }
            } else {
                auto f = cur.GetField(e->name.c_str());
                if (f.IsValid()) { writeField(e->ftype, cur, obj, e->name, nullptr, e->val); return; }
            }
        }
    } catch(...) {}
}

static std::string loopListJson() {
    std::lock_guard<std::mutex> lk(g_loopMtx);
    std::ostringstream j;
    j << "[";
    bool first = true;
    for (auto& kv : g_loops) {
        if (!first) j << ","; first = false;
        auto& e = kv.second;
        j << "{\"id\":\"" << jsEsc(kv.first) << "\",\"cls\":\"" << jsEsc(e->cls) << "\",\"name\":\"" << jsEsc(e->name) << "\",\"val\":\"" << jsEsc(e->val) << "\",\"interval\":" << e->intervalMs << ",\"active\":" << (e->active?"true":"false") << "}";
    }
    j << "]";
    return j.str();
}

#define MAX_CATCHERS 64
static std::shared_ptr<CatcherEntry>* g_catcherSlots[MAX_CATCHERS] = {};
static int g_catcherSlotCount = 0;
static std::mutex g_catcherSlotMtx;
static void (*g_catcherOrigPtrs[MAX_CATCHERS])(Il2CppObject*) = {};

template<int N>
static void catcherHook(Il2CppObject* self) {
    std::shared_ptr<CatcherEntry>* slot = g_catcherSlots[N];
    if (slot && *slot && (*slot)->active) {
        CatcherCall call;
        call.ts = nowSeconds();
        char buf[32]; snprintf(buf, sizeof(buf), "0x%llX", (unsigned long long)(uintptr_t)self);
        call.instance = buf;
        if (self && self->klass) {
            try {
                Method<BNM::Structures::Mono::String*> gn(Class("UnityEngine","Object").GetMethod("get_name"));
                if (gn.IsValid()) { auto* s = gn[self](); if (s) call.instance += " (" + s->str() + ")"; }
            } catch(...) {}
        }
        std::lock_guard<std::mutex> lk((*slot)->callsMtx);
        if ((*slot)->calls.size() >= CatcherEntry::MAX_CALLS) (*slot)->calls.erase((*slot)->calls.begin());
        (*slot)->calls.push_back(std::move(call));
    }
    if (g_catcherOrigPtrs[N]) g_catcherOrigPtrs[N](self);
}

typedef void(*CatcherHookFn)(Il2CppObject*);
static CatcherHookFn g_catcherHookFns[MAX_CATCHERS] = {
        catcherHook<0>,catcherHook<1>,catcherHook<2>,catcherHook<3>,catcherHook<4>,
        catcherHook<5>,catcherHook<6>,catcherHook<7>,catcherHook<8>,catcherHook<9>,
        catcherHook<10>,catcherHook<11>,catcherHook<12>,catcherHook<13>,catcherHook<14>,
        catcherHook<15>,catcherHook<16>,catcherHook<17>,catcherHook<18>,catcherHook<19>,
        catcherHook<20>,catcherHook<21>,catcherHook<22>,catcherHook<23>,catcherHook<24>,
        catcherHook<25>,catcherHook<26>,catcherHook<27>,catcherHook<28>,catcherHook<29>,
        catcherHook<30>,catcherHook<31>,catcherHook<32>,catcherHook<33>,catcherHook<34>,
        catcherHook<35>,catcherHook<36>,catcherHook<37>,catcherHook<38>,catcherHook<39>,
        catcherHook<40>,catcherHook<41>,catcherHook<42>,catcherHook<43>,catcherHook<44>,
        catcherHook<45>,catcherHook<46>,catcherHook<47>,catcherHook<48>,catcherHook<49>,
        catcherHook<50>,catcherHook<51>,catcherHook<52>,catcherHook<53>,catcherHook<54>,
        catcherHook<55>,catcherHook<56>,catcherHook<57>,catcherHook<58>,catcherHook<59>,
        catcherHook<60>,catcherHook<61>,catcherHook<62>,catcherHook<63>
};

// GameManager by the object itself (131b19): an edit that only names an address (scene
// update/destroy, addcomponent, invoke, loop add) carries no class name, so the request
// text check alone let it through. Resolve the address and look at its class chain.
static bool objectIsGameManager(const std::string& addrHex) {
    if (addrHex.empty()) return false;
    uintptr_t addr = (uintptr_t)strtoull(addrHex.c_str(), nullptr, 16);
    if (!addr) return false;
    Il2CppObject* obj = resolveComponentByAddr(addr);
    if (!obj || !obj->klass) return false;
    for (Class cur(obj->klass); cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
        std::string low;
        for (const char* p = cur.GetClass()->name; p && *p; ++p) low.push_back((char)tolower((unsigned char)*p));
        if (low.find("gamemanager") != std::string::npos) return true;
    }
    return false;
}

static void startServer() {
    httplib::Server svr;
    // Any explorer write whose request mentions GameManager is refused (scene/instance
    // update and delete); reads and other writes work as before.
    svr.set_pre_routing_handler([](const httplib::Request& req, httplib::Response& res) {
        if (req.method != "GET") {
            std::string low;
            low.reserve(req.body.size());
            for (char c : req.body) low.push_back((char)tolower((unsigned char)c));
            if (low.find("gamemanager") != std::string::npos || objectIsGameManager(jsonVal(req.body, "addr"))) {
                res.status = 403;
                res.set_content("{\"ok\":false,\"error\":\"GameManager is locked\"}", "application/json");
                return httplib::Server::HandlerResponse::Handled;
            }
        }
        return httplib::Server::HandlerResponse::Unhandled;
    });

    svr.Get("/",[](const httplib::Request&, httplib::Response& res) {
        int n = 0;
        for (auto& img : Image::GetImages()) if (img.IsValid()) n++;
        res.set_content(GetExplorerHTML(n), "text/html");
    });

    svr.Get("/api/assemblies",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(assembliesJson(), "application/json");
    });

    svr.Get("/api/classes",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(classesJson(req.get_param_value("a")), "application/json");
    });

    svr.Get("/api/allclasses",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(allClassesJson(), "application/json");
    });

    svr.Get("/api/class",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(classDetailJson(req.get_param_value("a"), req.get_param_value("ns"), req.get_param_value("n")), "application/json");
    });

    svr.Get("/api/instances",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        res.set_content(instancesJson(req.get_param_value("a"), req.get_param_value("ns"), req.get_param_value("n")), "application/json");
    });

    svr.Get("/api/scene",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        res.set_content(sceneJson(), "application/json");
    });

    svr.Get("/api/scene/inspect",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = req.get_param_value("addr");
        res.set_content(s.empty() ? "{}" : goInfoJson((uintptr_t)strtoull(s.c_str(), nullptr, 16)), "application/json");
    });

    svr.Get("/api/controller/inspect",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(req.get_param_value("addr").c_str(), nullptr, 16);
        std::string a = req.get_param_value("asm"), ns = req.get_param_value("ns"), cn = req.get_param_value("cls");

        Class startCls(ns.c_str(), cn.c_str(), Image(a.c_str()));
        if (!startCls.IsValid()) { res.set_content("{}", "application/json"); return; }

        std::ostringstream j;
        j << "{\"addr\":\"" << std::hex << addr << "\",\"name\":\"" << jsEsc(cn) << "\",\"fields\":[";

        bool firstF = true;
        std::vector<std::string> seen;

        for (Class cur = startCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
            auto* cn_ = cur.GetClass()->name;
            if (!cn_) break;
            if (strcmp(cn_, "Object") == 0 && cur.GetClass()->namespaze && strcmp(cur.GetClass()->namespaze, "System") == 0) break;

            for (auto& f : cur.GetFields(false)) {
                try {
                    auto* fi = f.GetInfo();
                    if (!fi || !fi->type) continue;
                    bool fStatic = (fi->type->attrs & 0x10) != 0;
                    if (!fStatic && !addr) continue;
                    std::string fn = fi->name;
                    bool dup = false; for (auto& s : seen) if (s==fn) dup=true;
                    if (dup) continue; seen.push_back(fn);
                    std::string ft = typeName(fi->type);
                    bool ok = false;
                    std::string vs = readField(ft, cur, fStatic ? nullptr : (Il2CppObject*)addr, fn, nullptr, fStatic, ok);
                    if (ok) { if (!firstF) j << ","; firstF = false; j << "{\"name\":\"" << jsEsc(fn) << "\",\"type\":\"" << ft << "\",\"val\":" << vs << ",\"isProp\":false,\"canWrite\":true,\"static\":" << (fStatic?"true":"false") << "}"; }
                } catch(...) {}
            }

            for (auto& p : cur.GetProperties(false)) {
                try {
                    auto* pi = p._data;
                    if (!pi || !pi->get) continue;
                    bool pStatic = (pi->get->flags & 0x10) != 0;
                    if (!pStatic && !addr) continue;
                    std::string pn = pi->name;
                    bool dup = false; for (auto& s : seen) if (s==pn) dup=true;
                    if (dup) continue; seen.push_back(pn);
                    std::string pt = typeName(pi->get->return_type);
                    bool ok = false;
                    MethodBase getter(pi->get);
                    std::string vs = readField(pt, cur, pStatic ? nullptr : (Il2CppObject*)addr, pn, &getter, pStatic, ok);
                    if (ok) { if (!firstF) j << ","; firstF = false; j << "{\"name\":\"" << jsEsc(pn) << "\",\"type\":\"" << pt << "\",\"val\":" << vs << ",\"isProp\":true,\"canWrite\":" << (pi->set?"true":"false") << ",\"static\":" << (pStatic?"true":"false") << "}"; }
                } catch(...) {}
            }
        }

        j << "],\"methods\":[";
        bool firstM = true;
        for (Class cur = startCls; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
            auto* cn_ = cur.GetClass()->name;
            if (!cn_) break;
            if (strcmp(cn_, "Object") == 0 && cur.GetClass()->namespaze && strcmp(cur.GetClass()->namespaze, "System") == 0) break;
            for (auto& m : cur.GetMethods(false)) {
                auto* mi = m.GetInfo();
                if (!m.IsValid() || !mi || !mi->name) continue;
                if (!firstM) j << ","; firstM = false;
                j << "{\"name\":\"" << jsEsc(mi->name) << "\",\"ret\":\"" << jsEsc(typeName(mi->return_type)) << "\",\"s\":" << ((mi->flags&0x10)?"true":"false") << ",\"params\":[";
                for (int i = 0; i < (int)mi->parameters_count; i++) {
                    if (i>0) j<<",";
                    j << "{\"n\":\"arg" << i << "\",\"t\":\"" << jsEsc(mi->parameters&&mi->parameters[i]?typeName(mi->parameters[i]):"?") << "\"}";
                }
                j << "]}";
            }
        }
        j << "]}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/scene/update",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        if (!addr) { res.set_content("{\"ok\":false}", "application/json"); return; }
        std::string type = jsonVal(req.body, "type"), prop = jsonVal(req.body, "prop"), val = jsonVal(req.body, "val");
        try {
            if (type == "gameobject") {
                if (prop == "active") {
                    bool newActive = val == "true";
                    Il2CppObject* go = resolveComponentByAddr(addr);
                    if (go) {
                        Method<void> setActive(Class("UnityEngine","GameObject").GetMethod("SetActive", 1));
                        if (setActive.IsValid()) setActive[go](newActive);
                    }
                } else if (prop == "name") {
                    Il2CppObject* go = resolveComponentByAddr(addr);
                    if (go) {
                        Method<void> m(Class("UnityEngine","Object").GetMethod("set_name",1));
                        if(m.IsValid()) m[go](CreateMonoString(val));
                    }
                }
            } else if (type == "transform") {
                float x=0,y=0,z=0; sscanf(val.c_str(),"[%f,%f,%f]",&x,&y,&z); Vector3 v={x,y,z};
                Il2CppObject* tr = resolveComponentByAddr(addr);
                if (tr) {
                    if (prop=="p") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localPosition",1)); if(m.IsValid()) m[tr](v); }
                    else if (prop=="r") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localEulerAngles",1)); if(m.IsValid()) m[tr](v); }
                    else if (prop=="s") { Method<void> m(Class("UnityEngine","Transform").GetMethod("set_localScale",1)); if(m.IsValid()) m[tr](v); }
                }
            } else if (type == "script") {
                std::string name = jsonVal(req.body, "prop2");
                std::string ft   = jsonVal(req.body, "ftype");
                bool isProp      = jsonVal(req.body, "isProp") == "true";
                if (prop == "enabled") {
                    try {
                        bool newEnabled = val == "true";
                        Il2CppObject* comp = resolveComponentByAddr(addr);
                        if (comp && comp->klass) {
                            auto setEnabledOnComp = [&](Il2CppObject* c, bool v) {
                                Class compKlass(c->klass);
                                for (Class cur = compKlass; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                                    auto* cn_ = cur.GetClass()->name;
                                    if (!cn_ || strcmp(cn_, "Component") == 0 || strcmp(cn_, "Object") == 0) break;
                                    auto ep = cur.GetProperty("enabled");
                                    if (ep.IsValid() && ep._data && ep._data->set) {
                                        Method<void> m(ep._data->set); m.SetInstance(c); m(v); return true;
                                    }
                                }
                                return false;
                            };
                            auto getEnabledFromComp = [&](Il2CppObject* c) -> bool {
                                Class compKlass(c->klass);
                                for (Class cur = compKlass; cur.IsValid() && cur.GetClass(); cur = cur.GetParent()) {
                                    auto* cn_ = cur.GetClass()->name;
                                    if (!cn_ || strcmp(cn_, "Component") == 0 || strcmp(cn_, "Object") == 0) break;
                                    auto ep = cur.GetProperty("enabled");
                                    if (ep.IsValid() && ep._data && ep._data->get) {
                                        Method<bool> m(ep._data->get); m.SetInstance(c);
                                        try { return m(); } catch(...) {}
                                    }
                                }
                                return false;
                            };
                            if (newEnabled) {
                                bool already = getEnabledFromComp(comp);
                                if (!already) {
                                    setEnabledOnComp(comp, true);
                                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                                    comp = resolveComponentByAddr(addr);
                                    if (!comp) { res.set_content("{\"ok\":true,\"warn\":\"enabled but object moved\"}", "application/json"); return; }
                                }
                            } else {
                                setEnabledOnComp(comp, false);
                            }
                        }
                    } catch(...) {}
                } else {
                    std::string compNs  = jsonVal(req.body, "compNs");
                    std::string compCls = jsonVal(req.body, "compCls");
                    std::string compAsm = jsonVal(req.body, "compAsm");
                    Il2CppObject* obj = resolveComponentByAddr(addr);
                    if (!obj) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
                    Class startCls = (obj->klass) ? Class(obj->klass) :
                                     Class(compNs.c_str(), compCls.c_str(), compAsm.empty() ? Image() : Image(compAsm.c_str()));
                    for (Class cur = startCls; cur.IsValid(); cur = cur.GetParent()) {
                        if (isProp) {
                            auto p = cur.GetProperty(name.c_str());
                            if (p.IsValid() && p._data && p._data->set) { MethodBase s(p._data->set); writeField(ft, cur, obj, name, &s, val); break; }
                        } else {
                            auto f = cur.GetField(name.c_str());
                            if (f.IsValid()) { writeField(ft, cur, obj, name, nullptr, val); break; }
                        }
                    }
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/instance/update",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        std::string a=jsonVal(req.body,"asm"), ns=jsonVal(req.body,"ns"), cn=jsonVal(req.body,"cls");
        std::string name=jsonVal(req.body,"name"), ft=jsonVal(req.body,"ftype"), val=jsonVal(req.body,"val");
        bool isProp = jsonVal(req.body,"isProp") == "true";
        bool isStaticField = jsonVal(req.body,"isStatic") == "true";
        Il2CppObject* obj = isStaticField ? nullptr : resolveComponentByAddr(addr);
        if (obj || isStaticField) {
            try {
                for (Class cur(ns.c_str(),cn.c_str(),Image(a.c_str())); cur.IsValid(); cur = cur.GetParent()) {
                    if (isProp) {
                        auto p = cur.GetProperty(name.c_str());
                        if (p.IsValid() && p._data && p._data->set) { MethodBase s(p._data->set); writeField(ft,cur,obj,name,&s,val,isStaticField); break; }
                    } else {
                        auto f = cur.GetField(name.c_str());
                        if (f.IsValid()) { writeField(ft,cur,obj,name,nullptr,val,isStaticField); break; }
                    }
                }
            } catch(...) {}
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/delete",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = jsonVal(req.body, "addr");
        if (!s.empty()) {
            uintptr_t addr = (uintptr_t)strtoull(s.c_str(), nullptr, 16);
            Il2CppObject* obj = resolveComponentByAddr(addr);
            if (obj) {
                try { Method<void> m(Class("UnityEngine","Object").GetMethod("Destroy",1)); if(m.IsValid()) m(obj); } catch(...) {}
            }
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/invoke",[](const httplib::Request& req, httplib::Response& res) {
        auto r = invokeMethod(req.body);
        std::ostringstream j;
        j << "{\"ok\":" << (r.ok?"true":"false") << ",\"value\":\"" << jsEsc(r.val) << "\",\"error\":\"" << jsEsc(r.err) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/loop/add",[](const httplib::Request& req, httplib::Response& res) {
        auto e = std::make_shared<LoopEntry>();
        e->addr      = jsonVal(req.body, "addr");
        e->asm_      = jsonVal(req.body, "asm");
        e->ns        = jsonVal(req.body, "ns");
        e->cls       = jsonVal(req.body, "cls");
        e->name      = jsonVal(req.body, "name");
        e->ftype     = jsonVal(req.body, "ftype");
        e->isProp    = jsonVal(req.body, "isProp") == "true";
        e->val       = jsonVal(req.body, "val");
        int ms       = 0;
        try { ms = std::stoi(jsonVal(req.body, "interval")); } catch(...) {}
        if (ms < 50) ms = 100;
        e->intervalMs = ms;

        std::string id = e->addr + "_" + e->cls + "_" + e->name;
        {
            std::lock_guard<std::mutex> lk(g_loopMtx);
            auto it = g_loops.find(id);
            if (it != g_loops.end()) it->second->active = false;
            g_loops[id] = e;
        }

        std::thread([e, id]() {
            AttachThread();
            while (e->active) {
                applyLoopEntry(e);
                std::this_thread::sleep_for(std::chrono::milliseconds(e->intervalMs));
            }
            std::lock_guard<std::mutex> lk(g_loopMtx);
            auto it = g_loops.find(id);
            if (it != g_loops.end() && it->second == e) g_loops.erase(it);
        }).detach();

        std::ostringstream j;
        j << "{\"ok\":true,\"id\":\"" << jsEsc(id) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/loop/remove",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_loopMtx);
        auto it = g_loops.find(id);
        if (it != g_loops.end()) { it->second->active = false; g_loops.erase(it); }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/loop/removeall",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_loopMtx);
        for (auto& kv : g_loops) kv.second->active = false;
        g_loops.clear();
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/loop/list",[](const httplib::Request&, httplib::Response& res) {
        res.set_content(loopListJson(), "application/json");
    });

    svr.Get("/api/logs",[](const httplib::Request&, httplib::Response& res) {
        res.set_content(logsJson(), "application/json");
    });

    svr.Post("/api/logs/clear",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_logMtx);
        g_logBuffer.clear();
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/logs/add",[](const httplib::Request& req, httplib::Response& res) {
        std::string msg = jsonVal(req.body, "msg");
        int level = 0;
        try { level = std::stoi(jsonVal(req.body, "level")); } catch(...) {}
        addLogEntry(msg, level);
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/timescale",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        float ts = 1.0f;
        try {
            Class timeCls("UnityEngine", "Time");
            if (timeCls.IsValid()) {
                Method<float> get(timeCls.GetMethod("get_timeScale"));
                if (get.IsValid()) ts = get();
            }
        } catch(...) {}
        res.set_content("{\"value\":" + std::to_string(ts) + "}", "application/json");
    });

    svr.Post("/api/timescale",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        float val = 1.0f;
        try { val = std::stof(jsonVal(req.body, "value")); } catch(...) {}
        try {
            Class timeCls("UnityEngine", "Time");
            if (timeCls.IsValid()) {
                Method<void> set(timeCls.GetMethod("set_timeScale", 1));
                if (set.IsValid()) set(val);
            }
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/destroy",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        auto s = jsonVal(req.body, "addr");
        if (!s.empty()) {
            uintptr_t addr = (uintptr_t)strtoull(s.c_str(), nullptr, 16);
            Il2CppObject* obj = resolveComponentByAddr(addr);
            if (obj) {
                try {
                    Method<void> m(Class("UnityEngine","Object").GetMethod("Destroy",1));
                    if (m.IsValid()) m(obj);
                } catch(...) {}
            }
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/scene/create",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        std::string name = jsonVal(req.body, "name");
        if (name.empty()) name = "New GameObject";
        std::ostringstream j;
        try {
            Class goCls("UnityEngine", "GameObject");
            Method<Il2CppObject*> ctor;
            for (auto& m : goCls.GetMethods(false)) {
                auto* mi = m.GetInfo();
                if (mi && mi->name && strcmp(mi->name, ".ctor") == 0 && mi->parameters_count == 1) {
                    ctor = Method<Il2CppObject*>(m);
                    break;
                }
            }
            if (ctor.IsValid()) {
                Il2CppObject* newGO = goCls.CreateNewObjectParameters(CreateMonoString(name.c_str()));
                if (newGO) {
                    j << "{\"ok\":true,\"addr\":\"" << std::hex << (uintptr_t)newGO << "\"}";
                    res.set_content(j.str(), "application/json");
                    return;
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":false,\"error\":\"Failed to create GameObject\"}", "application/json");
    });

    svr.Post("/api/scene/addcomponent",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        std::string typeName_ = jsonVal(req.body, "type");
        if (!addr || typeName_.empty()) { res.set_content("{\"ok\":false}", "application/json"); return; }
        Il2CppObject* go = resolveComponentByAddr(addr);
        if (!go) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
        try {
            Il2CppObject* typeObj = sysTypeOf(typeName_);
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", UnityEngine.CoreModule");
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", UnityEngine");
            if (!typeObj) typeObj = sysTypeOf(typeName_ + ", Assembly-CSharp");
            if (typeObj) {
                Class goCls("UnityEngine", "GameObject");
                Method<Il2CppObject*> addComp(goCls.GetMethod("AddComponent", 1));
                if (addComp.IsValid()) {
                    Il2CppObject* comp = addComp[go](typeObj);
                    if (comp) {
                        std::ostringstream j;
                        j << "{\"ok\":true,\"addr\":\"" << std::hex << (uintptr_t)comp << "\"}";
                        res.set_content(j.str(), "application/json");
                        return;
                    }
                }
            }
        } catch(...) {}
        res.set_content("{\"ok\":false,\"error\":\"Failed to add component\"}", "application/json");
    });

    svr.Post("/api/scene/removecomponent",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        uintptr_t addr = (uintptr_t)strtoull(jsonVal(req.body, "addr").c_str(), nullptr, 16);
        if (!addr) { res.set_content("{\"ok\":false}", "application/json"); return; }
        Il2CppObject* obj = resolveComponentByAddr(addr);
        if (!obj) { res.set_content("{\"ok\":false,\"error\":\"stale address\"}", "application/json"); return; }
        try {
            Method<void> destroy(Class("UnityEngine","Object").GetMethod("Destroy",1));
            if (destroy.IsValid()) destroy(obj);
        } catch(...) {}
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/dump",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        std::ostringstream out;
        for (auto& img : Image::GetImages()) {
            auto* d = img.GetInfo();
            if (!img.IsValid() || !d || !d->name) continue;
            out << "// Assembly: " << d->name << "\n\n";
            for (auto& cls : img.GetClasses(false)) {
                auto* r = cls.GetClass();
                if (!cls.IsValid() || !r || !r->name) continue;
                std::string clsKw = "class";
                if (r->enumtype) clsKw = "enum";
                else if (r->byval_arg.valuetype) clsKw = "struct";
                else if (r->flags & 0x20) clsKw = "interface";
                if (r->namespaze && strlen(r->namespaze) > 0) {
                    out << "namespace " << r->namespaze << " {\n";
                }
                std::string indent = (r->namespaze && strlen(r->namespaze) > 0) ? "    " : "";
                std::string parent = "";
                if (r->parent && r->parent->name && strcmp(r->parent->name, "Object") != 0 && strcmp(r->parent->name, "ValueType") != 0)
                    parent = " : " + std::string(r->parent->namespaze && strlen(r->parent->namespaze) > 0 ? std::string(r->parent->namespaze) + "." : "") + r->parent->name;
                out << indent << "public " << clsKw << " " << r->name << parent << " {\n";
                for (auto& f : cls.GetFields(false)) {
                    auto* fi = f.GetInfo();
                    if (!fi || !fi->name) continue;
                    std::string tn = typeName(fi->type);
                    bool isStatic = fi->type && (fi->type->attrs & 0x10);
                    out << indent << "    // offset 0x" << std::hex << fi->offset << std::dec << "\n";
                    out << indent << "    public " << (isStatic ? "static " : "") << tn << " " << fi->name << ";\n";
                }
                for (auto& p : cls.GetProperties(false)) {
                    auto* pi = p._data;
                    if (!pi || !pi->name) continue;
                    std::string tn = pi->get && pi->get->return_type ? typeName(pi->get->return_type) : "?";
                    out << indent << "    public " << tn << " " << pi->name << " {";
                    if (pi->get) out << " get;";
                    if (pi->set) out << " set;";
                    out << " }\n";
                }
                for (auto& m : cls.GetMethods(false)) {
                    auto* mi = m.GetInfo();
                    if (!mi || !mi->name) continue;
                    char ab[32] = {};
                    if (mi->methodPointer) snprintf(ab, sizeof(ab), "0x%llX", (unsigned long long)(uintptr_t)mi->methodPointer);
                    bool isStatic = mi->flags & 0x10;
                    out << indent << "    // " << ab << "\n";
                    out << indent << "    public " << (isStatic ? "static " : "") << typeName(mi->return_type) << " " << mi->name << "(";
                    for (int i = 0; i < (int)mi->parameters_count; i++) {
                        if (i > 0) out << ", ";
                        out << (mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?") << " arg" << i;
                    }
                    out << ") {}\n";
                }
                out << indent << "}\n";
                if (r->namespaze && strlen(r->namespaze) > 0) out << "}\n";
                out << "\n";
            }
        }
        res.set_content(out.str(), "text/plain");
    });

    svr.Get("/api/scene/addable",[](const httplib::Request&, httplib::Response& res) {
        AttachThread();
        std::ostringstream j;
        j << "{";
        bool firstCat = true;
        static const struct { const char* cat; const char* ns; const char* name; } known[] = {
                {"Physics","UnityEngine","Rigidbody"},{"Physics","UnityEngine","BoxCollider"},{"Physics","UnityEngine","SphereCollider"},
                {"Physics","UnityEngine","CapsuleCollider"},{"Physics","UnityEngine","MeshCollider"},{"Physics","UnityEngine","CharacterController"},
                {"Physics","UnityEngine","WheelCollider"},{"Physics","UnityEngine","Rigidbody2D"},{"Physics","UnityEngine","BoxCollider2D"},
                {"Physics","UnityEngine","CircleCollider2D"},{"Physics","UnityEngine","PolygonCollider2D"},{"Physics","UnityEngine","CapsuleCollider2D"},
                {"Rendering","UnityEngine","MeshRenderer"},{"Rendering","UnityEngine","MeshFilter"},{"Rendering","UnityEngine","SkinnedMeshRenderer"},
                {"Rendering","UnityEngine","SpriteRenderer"},{"Rendering","UnityEngine","LineRenderer"},{"Rendering","UnityEngine","TrailRenderer"},
                {"Rendering","UnityEngine","Camera"},{"Rendering","UnityEngine","Light"},
                {"Audio","UnityEngine","AudioSource"},{"Audio","UnityEngine","AudioListener"},{"Audio","UnityEngine","AudioReverbZone"},
                {"Animation","UnityEngine","Animator"},{"Animation","UnityEngine","Animation"},{"Animation","UnityEngine","ParticleSystem"},
                {"Navigation","UnityEngine","NavMeshAgent"},{"Navigation","UnityEngine","NavMeshObstacle"},
                {"UI","UnityEngine.UI","Text"},{"UI","UnityEngine.UI","Image"},{"UI","UnityEngine.UI","Button"},
                {"UI","UnityEngine.UI","Toggle"},{"UI","UnityEngine.UI","Slider"},{"UI","UnityEngine.UI","InputField"},
                {"UI","UnityEngine.UI","Canvas"},{"UI","UnityEngine","CanvasGroup"},
                {nullptr,nullptr,nullptr}
        };
        std::map<std::string, std::vector<std::pair<std::string,std::string>>> cats;
        for (int i = 0; known[i].cat; i++) {
            Class c(known[i].ns, known[i].name);
            if (c.IsValid()) cats[known[i].cat].push_back({std::string(known[i].ns) + "." + known[i].name, known[i].name});
        }
        for (auto& img : Image::GetImages()) {
            auto* d = img.GetInfo();
            if (!img.IsValid() || !d || !d->name) continue;
            if (strstr(d->name, "Assembly-CSharp") == nullptr && strstr(d->name, "Assembly-CSharp-firstpass") == nullptr) continue;
            for (auto& cls : img.GetClasses(false)) {
                auto* r = cls.GetClass();
                if (!cls.IsValid() || !r || !r->name) continue;
                bool isMono = false;
                for (auto* cur = r; cur; cur = cur->parent)
                    if (cur->name && strcmp(cur->name, "MonoBehaviour") == 0) { isMono = true; break; }
                if (!isMono) continue;
                std::string ns = r->namespaze ? r->namespaze : "";
                std::string fullName = ns.empty() ? r->name : ns + "." + r->name;
                cats["Scripts"].push_back({fullName, r->name});
            }
        }
        for (auto& kv : cats) {
            if (!firstCat) j << ","; firstCat = false;
            j << "\"" << jsEsc(kv.first) << "\":[";
            bool first = true;
            for (auto& p : kv.second) {
                if (!first) j << ","; first = false;
                j << "{\"full\":\"" << jsEsc(p.first) << "\",\"name\":\"" << jsEsc(p.second) << "\"}";
            }
            j << "]";
        }
        j << "}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/catcher/hook",[](const httplib::Request& req, httplib::Response& res) {
        AttachThread();
        std::string asm_ = jsonVal(req.body, "asm");
        std::string ns   = jsonVal(req.body, "ns");
        std::string cls  = jsonVal(req.body, "cls");
        std::string meth = jsonVal(req.body, "method");
        std::string id   = asm_ + "|" + ns + "|" + cls + "|" + meth;
        {
            std::lock_guard<std::mutex> lk(g_catcherMtx);
            if (g_catchers.count(id)) {
                res.set_content(std::string("{\"ok\":true,\"exists\":true,\"id\":\"") + jsEsc(id) + "\"}", "application/json");
                return;
            }
        }
        int slot = -1;
        {
            std::lock_guard<std::mutex> lk(g_catcherSlotMtx);
            for (int i = 0; i < MAX_CATCHERS; i++) {
                if (!g_catcherSlots[i]) { slot = i; break; }
            }
        }
        if (slot < 0) { res.set_content("{\"ok\":false,\"error\":\"max catchers reached\"}", "application/json"); return; }
        auto entry = std::make_shared<CatcherEntry>();
        entry->id = id; entry->asm_ = asm_; entry->ns = ns; entry->cls = cls; entry->method = meth;
        entry->slotIdx = slot;
        Class c(ns.c_str(), cls.c_str(), Image(asm_.c_str()));
        if (!c.IsValid()) { res.set_content("{\"ok\":false,\"error\":\"class not found\"}", "application/json"); return; }
        MethodBase mb = c.GetMethod(meth.c_str());
        if (!mb.IsValid()) { res.set_content("{\"ok\":false,\"error\":\"method not found\"}", "application/json"); return; }
        auto* mi = mb.GetInfo();
        if (!mi || !mi->methodPointer) { res.set_content("{\"ok\":false,\"error\":\"no method pointer\"}", "application/json"); return; }
        entry->retType = typeName(mi->return_type);
        for (int i = 0; i < (int)mi->parameters_count; i++) {
            entry->paramTypes.push_back(mi->parameters && mi->parameters[i] ? typeName(mi->parameters[i]) : "?");
            entry->paramNames.push_back("arg" + std::to_string(i));
        }
        {
            std::lock_guard<std::mutex> lk(g_catcherSlotMtx);
            g_catcherSlots[slot] = new std::shared_ptr<CatcherEntry>(entry);
        }
        InvokeHook(mb, g_catcherHookFns[slot], g_catcherOrigPtrs[slot]);
        {
            std::lock_guard<std::mutex> lk(g_catcherMtx);
            g_catchers[id] = entry;
        }
        std::ostringstream j;
        j << "{\"ok\":true,\"id\":\"" << jsEsc(id) << "\"}";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/catcher/unhook",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it != g_catchers.end()) {
            it->second->active = false;
            int sl = it->second->slotIdx;
            if (sl >= 0 && sl < MAX_CATCHERS) {
                std::lock_guard<std::mutex> lk2(g_catcherSlotMtx);
                delete g_catcherSlots[sl];
                g_catcherSlots[sl] = nullptr;
                g_catcherOrigPtrs[sl] = nullptr;
            }
            g_catchers.erase(it);
        }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Post("/api/catcher/clear",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = jsonVal(req.body, "id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it != g_catchers.end()) { std::lock_guard<std::mutex> lk2(it->second->callsMtx); it->second->calls.clear(); }
        res.set_content("{\"ok\":true}", "application/json");
    });

    svr.Get("/api/catcher/list",[](const httplib::Request&, httplib::Response& res) {
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        std::ostringstream j;
        j << "[";
        bool first = true;
        for (auto& kv : g_catchers) {
            auto& e = kv.second;
            if (!first) j << ","; first = false;
            j << "{\"id\":\"" << jsEsc(e->id) << "\",\"cls\":\"" << jsEsc(e->cls) << "\",\"ns\":\"" << jsEsc(e->ns) << "\",\"method\":\"" << jsEsc(e->method) << "\",\"ret\":\"" << jsEsc(e->retType) << "\",\"active\":" << (e->active?"true":"false") << ",\"count\":";
            { std::lock_guard<std::mutex> lk2(e->callsMtx); j << e->calls.size(); }
            j << ",\"params\":[";
            for (size_t i = 0; i < e->paramTypes.size(); i++) {
                if (i>0) j<<",";
                j << "{\"t\":\"" << jsEsc(e->paramTypes[i]) << "\",\"n\":\"" << jsEsc(e->paramNames[i]) << "\"}";
            }
            j << "]}";
        }
        j << "]";
        res.set_content(j.str(), "application/json");
    });

    svr.Get("/api/catcher/calls",[](const httplib::Request& req, httplib::Response& res) {
        std::string id = req.get_param_value("id");
        std::lock_guard<std::mutex> lk(g_catcherMtx);
        auto it = g_catchers.find(id);
        if (it == g_catchers.end()) { res.set_content("[]", "application/json"); return; }
        auto& e = it->second;
        std::lock_guard<std::mutex> lk2(e->callsMtx);
        std::ostringstream j;
        j << "[";
        bool first = true;
        for (auto& c : e->calls) {
            if (!first) j << ","; first = false;
            j << "{\"ts\":" << std::fixed << c.ts << ",\"instance\":\"" << jsEsc(c.instance) << "\",\"args\":[";
            for (size_t i = 0; i < c.args.size(); i++) {
                if (i>0) j<<",";
                j << "{\"t\":\"" << jsEsc(c.args[i].type) << "\",\"v\":" << c.args[i].val << "}";
            }
            j << "],\"ret\":\"" << jsEsc(c.ret) << "\"}";
        }
        j << "]";
        res.set_content(j.str(), "application/json");
    });

    svr.Post("/api/lua/exec",[](const httplib::Request& req, httplib::Response& res) {
        try {
            std::string code = jsonVal(req.body, "code");
            if (code.empty()) { res.set_content("{\"output\":\"Error: empty code\"}", "application/json"); return; }
            std::string out = executeLuaBNM(code);
            std::ostringstream j;
            j << "{\"output\":\"" << jsEsc(out) << "\"}";
            res.set_content(j.str(), "application/json");
        } catch (...) {
            res.set_content("{\"output\":\"Error: native crash in executor\"}", "application/json");
        }
    });

    // Bind to localhost ONLY (not 0.0.0.0) so the Explorer is reachable only
    // on-device (adb forward tcp:... tcp:8080 still works for debugging) and
    // NOT by anyone else on the player's network — closing a live-game
    // inspect/edit hole in the public build.
    svr.listen("127.0.0.1", g_port);
}

// --- Entitlement flow observation hooks ---------------------------------
// Armed in-process from OnLoaded(), before any HTTP round trip is possible,
// so we don't lose the race against Unity's Awake()/Start() lifecycle (which
// fires within milliseconds of process resume - manually arming hooks over
// curl after the fact is consistently too slow to catch it).
//
// Uses BasicHook (Dobby inline hook on the real function address) rather
// than BNM's InvokeHook/MethodInfo-swap technique or the built-in "catcher"
// system: InvokeHook only catches calls that go through il2cpp API method
// dispatch (reflection/runtime_invoke), not direct compiled call sites,
// which is how normal C# method calls are actually emitted. The built-in
// catcher only forwards `self` (no other args), which would corrupt any
// call to a method that takes real parameters - unsafe for these targets.
//
// Native IL2CPP ABI (see BNM::Method::Call in Method.hpp): non-static
// methods are `Ret(*)(Il2CppObject*, Params..., MethodInfo*)`; static
// methods (UNITY_VER > 174, true here) are `Ret(*)(Params..., MethodInfo*)`.
// Reference-type params (objects, delegates, List<T>) are all pointer-sized
// so treating unknown/obfuscated param types generically as void* is ABI-safe.


// =====================================================================
//  YEEPS 1.19.0 (Unity 2021.3.19f1) — Oculus Platform entitlement bypass
// ---------------------------------------------------------------------
//  Ground truth from on-device logcat (com.TrassGames.Yeeps 1.19.0):
//    * "Entitlement: Beginning entitlement check"
//    * "Entitlement: Received check callback"          <- ENTITLEMENT PASSES
//    * "Entitlement: Failed to get user age group"     <- 400 Invalid OAuth
//    * "Loading Screen: Getting user proof..."
//    * "Entitlement: Failed to get user proof: 400"    <- 400 Invalid OAuth  <== THE GATE
//  After the proof fails the account never goes online, NetworkManager
//  never connects to Photon, and the client stalls on the loading screen.
//
//  The proof / age / nonce results are delivered through the NATIVE Oculus
//  Platform SDK (libovrplatformloader.so) and read by the managed C# entitlement
//  callbacks via P/Invoke of plain C exports. Those exports are name-stable
//  across builds, so we hook them directly with Dobby — no IL2CPP metadata /
//  obfuscated-name resolution needed for the core fix.
//
//  ovrMessageType constants (from the 1.19.0 il2cpp metadata dump,
//  Oculus.Platform.MessageType enum — the member NAMES survive obfuscation):
//     User_GetUserProof            = 578880643
//     UserAgeCategory_Get          = 567009472
//     User_GetLoggedInUser         = 1131361373
//
//  For an errored proof/age message we force ovr_Message_IsError -> false and
//  hand the managed callback a fabricated (non-null) handle whose getter returns
//  a dummy value, so the callback takes its SUCCESS branch. The dummy nonce is
//  then sent to the Yeeps backend (redirected to the self-hosted server via the
//  metadata URL patch), which does not validate it against Meta.
// =====================================================================

#include "extern/Dobby/include/dobby.h"

static const int OVRMSG_User_GetUserProof    = 578880643;
static const int OVRMSG_UserAgeCategory_Get  = 567009472;
// Meta friends ("me/friends") always fails with HTTP 400 for this build, which
// made the game pop "Failed to get friends: 400" for everyone. Treat it as a
// successful, EMPTY friend list instead (temporary in-game friends still work).
static const int OVRMSG_User_GetLoggedInUserFriends = 1484532365;
static const int OVRMSG_User_GetNextUserArrayPage   = 645723971;
static char g_fakeUserArray[8] = {0};

typedef unsigned char ovrBool;
typedef int           (*ovr_GetType_t)(void* msg);
typedef ovrBool       (*ovr_IsError_t)(void* msg);
typedef void*         (*ovr_GetUserProof_t)(void* msg);
typedef const char*   (*ovr_GetNonce_t)(void* proof);
typedef void*         (*ovr_GetAgeMsg_t)(void* msg);
typedef int           (*ovr_GetAgeCat_t)(void* handle);

static ovr_GetType_t      real_GetType     = nullptr;   // called, never hooked
static ovr_IsError_t      real_IsError     = nullptr;
static ovr_GetUserProof_t real_GetUserProof= nullptr;
static ovr_GetNonce_t     real_GetNonce    = nullptr;
static ovr_GetAgeMsg_t    real_GetAgeMsg   = nullptr;
static ovr_GetAgeCat_t    real_GetAgeCat   = nullptr;

// Sentinel handles handed back for errored messages (must be non-null & unique).
static char        g_fakeProofHandle[8] = {0};
static char        g_fakeAgeHandle[8]   = {0};
// A plausible 64-hex-char nonce. The redirected backend accepts any value.
static const char* g_dummyNonce = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90";

// ---- friends diagnostics (131b16) --------------------------------------
// Every Meta platform answer the game reads (type id, ok/error, error code,
// HTTP code and message) goes into the game's own log via Debug.Log, so the
// in-game debug report carries it (owner asked for more info to find the
// "adding friends" problem). First few of each type only, so it can't flood.
typedef void*       (*ovr_GetError_t)(void* msg);
typedef const char* (*ovr_ErrMsg_t)(void* err);
typedef int         (*ovr_ErrInt_t)(void* err);
static ovr_GetError_t real_GetError = nullptr;
static ovr_ErrMsg_t   real_ErrMsg   = nullptr;
static ovr_ErrInt_t   real_ErrHttp  = nullptr;
static ovr_ErrInt_t   real_ErrCode  = nullptr;

static void GameLog(const std::string& s) {
    BNM_FileCheckpoint(s.c_str());
    try {
        Method<void> logm(Class("UnityEngine", "Debug").GetMethod("Log", 1));
        if (logm.IsValid()) logm((Il2CppObject*)CreateMonoString(s.c_str()));
    } catch (...) {}
}

static const char* OvrTypeName(int t) {
    switch (t) {
        case 578880643:  return "User_GetUserProof";
        case 567009472:  return "UserAgeCategory_Get";
        case 1131361373: return "User_GetLoggedInUser";
        case 1484532365: return "User_GetLoggedInUserFriends";
        case 645723971:  return "User_GetNextUserArrayPage";
        default:         return "?";
    }
}

static void LogOvrMessage(void* msg, int t, bool err) {
    static std::mutex mtx;
    static std::map<int, int> seen;
    { std::lock_guard<std::mutex> lk(mtx); if (seen[t]++ >= 4) return; }
    char buf[512];
    if (err && real_GetError) {
        void* e = real_GetError(msg);
        const char* m = (e && real_ErrMsg) ? real_ErrMsg(e) : nullptr;
        int http = (e && real_ErrHttp) ? real_ErrHttp(e) : 0;
        int code = (e && real_ErrCode) ? real_ErrCode(e) : 0;
        snprintf(buf, sizeof(buf), "[FriendsDiag] Meta msg %d (%s) ERROR code=%d http=%d: %.300s",
                 t, OvrTypeName(t), code, http, m ? m : "(no message)");
    } else {
        snprintf(buf, sizeof(buf), "[FriendsDiag] Meta msg %d (%s) ok", t, OvrTypeName(t));
    }
    GameLog(buf);
}

static ovrBool hook_ovr_Message_IsError(void* msg) {
    ovrBool err = real_IsError ? real_IsError(msg) : (ovrBool)0;
    if (real_GetType) {
        try { LogOvrMessage(msg, real_GetType(msg), err != 0); } catch (...) {}
    }
    if (err && real_GetType) {
        int t = real_GetType(msg);
        if (t == OVRMSG_User_GetUserProof || t == OVRMSG_UserAgeCategory_Get ||
            t == OVRMSG_User_GetLoggedInUserFriends || t == OVRMSG_User_GetNextUserArrayPage) {
            BNM_FileCheckpointF("[ovr] IsError: forcing SUCCESS for msg type %d (real=error)", t);
            return (ovrBool)0;
        }
    }
    return err;
}

static void* hook_ovr_Message_GetUserProof(void* msg) {
    // If the underlying proof message really errored, its data pointer is
    // unsafe — return our sentinel so GetNonce yields the dummy nonce.
    if (real_IsError && real_IsError(msg)) {
        BNM_FileCheckpoint("[ovr] GetUserProof: errored msg -> sentinel handle");
        return (void*)g_fakeProofHandle;
    }
    void* p = real_GetUserProof ? real_GetUserProof(msg) : nullptr;
    return p ? p : (void*)g_fakeProofHandle;
}

static const char* hook_ovr_UserProof_GetNonce(void* proof) {
    if (proof == (void*)g_fakeProofHandle) {
        BNM_FileCheckpoint("[ovr] GetNonce: sentinel -> dummy nonce");
        return g_dummyNonce;
    }
    const char* n = real_GetNonce ? real_GetNonce(proof) : nullptr;
    return n ? n : g_dummyNonce;
}

static void* hook_ovr_Message_GetUserAccountAgeCategory(void* msg) {
    if (real_IsError && real_IsError(msg)) {
        BNM_FileCheckpoint("[ovr] GetAgeCategory msg: errored -> sentinel handle");
        return (void*)g_fakeAgeHandle;
    }
    void* h = real_GetAgeMsg ? real_GetAgeMsg(msg) : nullptr;
    return h ? h : (void*)g_fakeAgeHandle;
}

static int hook_ovr_UserAccountAgeCategory_GetAgeCategory(void* handle) {
    if (handle == (void*)g_fakeAgeHandle) {
        return 3; // Adult (enum: Unknown=0, Child=1, Teen=2, Adult=3)
    }
    return real_GetAgeCat ? real_GetAgeCat(handle) : 3;
}

typedef void*       (*ovr_GetUserArray_t)(void* msg);
typedef size_t      (*ovr_UA_GetSize_t)(void* arr);
typedef ovrBool     (*ovr_UA_HasNext_t)(void* arr);
typedef const char* (*ovr_UA_NextUrl_t)(void* arr);
static ovr_GetUserArray_t real_GetUserArray = nullptr;
static ovr_UA_GetSize_t   real_UA_GetSize   = nullptr;
static ovr_UA_HasNext_t   real_UA_HasNext   = nullptr;
static ovr_UA_NextUrl_t   real_UA_NextUrl   = nullptr;
static void* hook_ovr_Message_GetUserArray(void* msg) {
    if (real_IsError && real_IsError(msg)) return (void*)g_fakeUserArray;   // errored -> empty list
    void* a = real_GetUserArray ? real_GetUserArray(msg) : nullptr;
    return a ? a : (void*)g_fakeUserArray;
}
static size_t hook_ovr_UserArray_GetSize(void* arr) {
    if (arr == (void*)g_fakeUserArray) return 0;
    return real_UA_GetSize ? real_UA_GetSize(arr) : 0;
}
static ovrBool hook_ovr_UserArray_HasNextPage(void* arr) {
    if (arr == (void*)g_fakeUserArray) return (ovrBool)0;
    return real_UA_HasNext ? real_UA_HasNext(arr) : (ovrBool)0;
}
static const char* hook_ovr_UserArray_GetNextUrl(void* arr) {
    if (arr == (void*)g_fakeUserArray) return "";
    return real_UA_NextUrl ? real_UA_NextUrl(arr) : "";
}

static bool g_ovrHooksInstalled = false;
static void InstallOvrPlatformHooks(void* lib) {
    if (g_ovrHooksInstalled) return;
    auto H = [&](const char* name, void* repl, void** orig) {
        void* sym = dlsym(lib, name);
        if (!sym) { BNM_FileCheckpointF("[ovr] dlsym FAILED: %s", name); return; }
        void* tramp = nullptr;
        if (DobbyHook(sym, (dobby_dummy_func_t)repl, (dobby_dummy_func_t*)&tramp) != 0) {
            BNM_FileCheckpointF("[ovr] DobbyHook FAILED: %s", name);
            return;
        }
        if (orig) *orig = tramp;
        BNM_FileCheckpointF("[ovr] hooked %s @ %p (tramp=%p)", name, sym, tramp);
    };
    // GetType is only called, not hooked.
    real_GetType = (ovr_GetType_t)dlsym(lib, "ovr_Message_GetType");
    BNM_FileCheckpointF("[ovr] ovr_Message_GetType @ %p", (void*)real_GetType);
    real_GetError = (ovr_GetError_t)dlsym(lib, "ovr_Message_GetError");
    real_ErrMsg   = (ovr_ErrMsg_t)dlsym(lib, "ovr_Error_GetMessage");
    real_ErrHttp  = (ovr_ErrInt_t)dlsym(lib, "ovr_Error_GetHttpCode");
    real_ErrCode  = (ovr_ErrInt_t)dlsym(lib, "ovr_Error_GetCode");
    BNM_FileCheckpointF("[ovr] error getters %p %p %p %p", (void*)real_GetError, (void*)real_ErrMsg,
                        (void*)real_ErrHttp, (void*)real_ErrCode);
    H("ovr_Message_IsError",                      (void*)hook_ovr_Message_IsError,                      (void**)&real_IsError);
    H("ovr_Message_GetUserProof",                 (void*)hook_ovr_Message_GetUserProof,                 (void**)&real_GetUserProof);
    H("ovr_UserProof_GetNonce",                   (void*)hook_ovr_UserProof_GetNonce,                   (void**)&real_GetNonce);
    H("ovr_Message_GetUserAccountAgeCategory",    (void*)hook_ovr_Message_GetUserAccountAgeCategory,    (void**)&real_GetAgeMsg);
    H("ovr_UserAccountAgeCategory_GetAgeCategory",(void*)hook_ovr_UserAccountAgeCategory_GetAgeCategory,(void**)&real_GetAgeCat);
    H("ovr_Message_GetUserArray",                 (void*)hook_ovr_Message_GetUserArray,                 (void**)&real_GetUserArray);
    H("ovr_UserArray_GetSize",                    (void*)hook_ovr_UserArray_GetSize,                    (void**)&real_UA_GetSize);
    H("ovr_UserArray_HasNextPage",                (void*)hook_ovr_UserArray_HasNextPage,                (void**)&real_UA_HasNext);
    H("ovr_UserArray_GetNextUrl",                 (void*)hook_ovr_UserArray_GetNextUrl,                 (void**)&real_UA_NextUrl);
    g_ovrHooksInstalled = true;
    BNM_FileCheckpoint("[ovr] InstallOvrPlatformHooks: DONE");
}

// libovrplatformloader.so is dlopen'd by the app during OVRPlatform init, which
// happens a little after process start. Poll (without forcing a load) until it
// appears, then install the hooks before the entitlement callbacks fire.
static void OvrHookPollThread() {
    // Poll effectively forever (process lifetime). The game may sit on the
    // Terms-of-Service / boot screens for an unbounded time before the game
    // scene loads and OVRPlatform (libovrplatformloader.so) is dlopen'd, so a
    // short timeout can miss it entirely on a fresh install. 100ms polling for
    // up to ~1 hour costs nothing and guarantees we catch the lib whenever it
    // loads, before the entitlement callbacks fire.
    for (int i = 0; i < 36000; i++) { // up to ~1 h
        void* lib = dlopen("libovrplatformloader.so", RTLD_NOLOAD | RTLD_NOW);
        if (lib) {
            BNM_FileCheckpointF("[ovr] libovrplatformloader.so present (attempt %d)", i);
            InstallOvrPlatformHooks(lib);
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    BNM_FileCheckpoint("[ovr] libovrplatformloader.so never appeared (1h timeout)");
}

// ---------------------------------------------------------------------
//  CREDENTIAL-DECRYPT DIAGNOSTICS (1.19.0)
//  The client decrypts e_accessKey/e_secretKey with a symmetric key it
//  holds; "Account: Failed to get credentials" fires when that fails. We
//  don't know 1.19.0's baked key (it differs from 2.4.0's 163eec...). Hook
//  the .NET crypto (mscorlib, plaintext names) to dump the exact key/IV and
//  the base64 blobs being decoded, so the server can re-encrypt the real
//  creds with the SAME key. il2cpp byte[] layout: length @ +0x18, data @ +0x20.
// ---------------------------------------------------------------------
static std::string HexDumpArr(void* arr) {
    if (!arr) return "(null)";
    try {
        uintptr_t len = *(uintptr_t*)((uint8_t*)arr + 0x18);
        if (len > 4096) return "(len too big)";
        uint8_t* data = (uint8_t*)arr + 0x20;
        static const char* H = "0123456789abcdef";
        std::string s; s.reserve(len * 2);
        for (uintptr_t i = 0; i < len; i++) { s += H[data[i] >> 4]; s += H[data[i] & 0xf]; }
        return s;
    } catch (...) { return "(exc)"; }
}
static void (*orig_setKey)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_setKey(void* self, void* val, IL2CPP::MethodInfo* mi) {
    BNM_FileCheckpointF("CRYPTO set_Key: %s", HexDumpArr(val).c_str());
    if (orig_setKey) orig_setKey(self, val, mi);
}
static void (*orig_setIV)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_setIV(void* self, void* val, IL2CPP::MethodInfo* mi) {
    BNM_FileCheckpointF("CRYPTO set_IV : %s", HexDumpArr(val).c_str());
    if (orig_setIV) orig_setIV(self, val, mi);
}
static void (*orig_rfcCtor)(void*, void*, void*, int, IL2CPP::MethodInfo*) = nullptr;
static void hook_rfcCtor(void* self, void* pwd, void* salt, int iters, IL2CPP::MethodInfo* mi) {
    std::string p;
    try { p = pwd ? ((BNM::Structures::Mono::String*)pwd)->str() : "(null)"; } catch(...) { p="(exc)"; }
    BNM_FileCheckpointF("CRYPTO Rfc2898.ctor: pwd='%s' salt=%s iters=%d", p.c_str(), HexDumpArr(salt).c_str(), iters);
    if (orig_rfcCtor) orig_rfcCtor(self, pwd, salt, iters, mi);
}
// Dump the base64 strings the client decodes (e_accessKey/e_secretKey land here).
static void* (*orig_FromBase64)(void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_FromBase64(void* s, IL2CPP::MethodInfo* mi) {
    try {
        std::string in = s ? ((BNM::Structures::Mono::String*)s)->str() : "(null)";
        if (in.size() >= 20 && in.size() <= 200)  // creds are 44-130 chars; skip tiny/huge
            BNM_FileCheckpointF("CRYPTO FromBase64('%s')", in.c_str());
    } catch(...) {}
    return orig_FromBase64 ? orig_FromBase64(s, mi) : nullptr;
}
// Dump the DECRYPTED global-metadata.dat from memory. il2cpp decrypts the
// on-disk (encrypted, header 0ac2246e) metadata into a heap buffer with the
// STANDARD magic AF 1B B1 FA during init. Scan /proc/self/maps ANONYMOUS rw
// regions for that magic and write the buffer to /sdcard so Il2CppDumper can
// parse it offline -> full 2.4.0 class/method symbol table (names -> offsets).
static void DumpDecryptedMetadata() {
    const char* OUT = "/sdcard/Android/data/com.TrassGames.Yeeps/files/meta_decrypted.dat";
    FILE* mf = fopen("/proc/self/maps", "r");
    if (!mf) { BNM_FileCheckpoint("MetaDump: cannot open maps"); return; }
    char line[512];
    const unsigned char M0=0xAF,M1=0x1B,M2=0xB1,M3=0xFA;
    int dumped = 0;
    while (fgets(line, sizeof(line), mf)) {
        unsigned long start=0, end=0; char perms[8]={0}; char path[256]={0};
        int n = sscanf(line, "%lx-%lx %7s %*x %*s %*u %255[^\n]", &start, &end, perms, path);
        if (n < 3) continue;
        if (perms[0] != 'r' || perms[1] != 'w') continue;   // heap: rw
        // skip file-backed mappings (incl. the ENCRYPTED global-metadata.dat file)
        if (n >= 4 && path[0] && strchr(path, '/')) continue;
        size_t sz = end - start;
        if (sz < 0x400000 || sz > 0x8000000) continue;       // metadata ~29MB
        unsigned char* p = (unsigned char*)start;
        size_t lim = sz - 4; if (lim > 0x2000000) lim = 0x2000000;
        for (size_t i = 0; i < lim; i++) {
            if (p[i]==M0 && p[i+1]==M1 && p[i+2]==M2 && p[i+3]==M3) {
                int ver = *(int*)(p+i+4);
                if (ver < 16 || ver > 40) continue;           // il2cpp metadata version sanity
                size_t avail = sz - i;
                size_t out = avail > 0x2800000 ? 0x2800000 : avail;  // cap ~40MB
                FILE* of = fopen(OUT, "wb");
                if (of) { fwrite(p+i, 1, out, of); fclose(of); dumped=1;
                    BNM_FileCheckpointF("MetaDump: WROTE %zu bytes ver=%d region=%lx-%lx OUT=%s", out, ver, start, end, OUT); }
                break;
            }
        }
        if (dumped) break;
    }
    fclose(mf);
    if (!dumped) BNM_FileCheckpoint("MetaDump: decrypted AF1BB1FA magic NOT FOUND in anon heap");
}

static void InstallCryptoKeyDump() {
    try {
        Class sa("System.Security.Cryptography", "SymmetricAlgorithm", Image("mscorlib.dll"));
        if (sa.IsValid()) {
            MethodBase k = sa.GetMethod("set_Key", 1);
            if (k.IsValid()) { BasicHook(k, hook_setKey, orig_setKey); BNM_FileCheckpoint("Crypto: set_Key hooked"); }
            else BNM_FileCheckpoint("Crypto: set_Key NOT FOUND");
            MethodBase v = sa.GetMethod("set_IV", 1);
            if (v.IsValid()) { BasicHook(v, hook_setIV, orig_setIV); BNM_FileCheckpoint("Crypto: set_IV hooked"); }
            else BNM_FileCheckpoint("Crypto: set_IV NOT FOUND");
        } else BNM_FileCheckpoint("Crypto: SymmetricAlgorithm NOT FOUND");
        Class rfc("System.Security.Cryptography", "Rfc2898DeriveBytes", Image("mscorlib.dll"));
        if (rfc.IsValid()) {
            MethodBase c = rfc.GetMethod(".ctor", 3);
            if (c.IsValid()) { BasicHook(c, hook_rfcCtor, orig_rfcCtor); BNM_FileCheckpoint("Crypto: Rfc2898.ctor(3) hooked"); }
        }
        Class conv("System", "Convert", Image("mscorlib.dll"));
        if (conv.IsValid()) {
            MethodBase m = conv.GetMethod("FromBase64String", 1);
            if (m.IsValid()) { BasicHook(m, hook_FromBase64, orig_FromBase64); BNM_FileCheckpoint("Crypto: FromBase64String hooked"); }
        }
    } catch (...) { BNM_FileCheckpoint("InstallCryptoKeyDump: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Credential-failure backtrace: hook LogManager.LogEvent(category, message)
//  and, when the credential/online-fetch errors fire, dump the libil2cpp
//  return-address backtrace as FILE OFFSETS (RVAs). 1.19.0 anchor:
//  il2cppBase = &il2cpp_array_new_specific - 0xebdc94. The topmost in-il2cpp
//  frame is the method that logged "Failed to get credentials" — disassemble
//  it to see exactly which response field read returns null.
// ---------------------------------------------------------------------
// While the AccountManager credential coroutine is processing the login
// payload we log every string field it reads, so we can see exactly which
// 3 fields its String.IsNullOrEmpty checks look at (and which is empty).
static std::atomic<bool> g_inCredProc{false};
// The account-data string getter AccountManager uses (RVA 0x1df5f90):
// System.String get(object obj, key, _, _). Logged only during cred processing.
static void* (*orig_credGetter)(void*, void*, void*, void*) = nullptr;
static void* hook_credGetter(void* obj, void* key, void* a2, void* a3) {
    void* ret = orig_credGetter ? orig_credGetter(obj, key, a2, a3) : nullptr;
    if (g_inCredProc.load()) {
        try {
            std::string k = key ? ((BNM::Structures::Mono::String*)key)->str() : "(nullkey)";
            std::string v = ret ? ((BNM::Structures::Mono::String*)ret)->str() : "(NULL)";
            if (k.size() < 60)
                BNM_FileCheckpointF("CREDGET '%s' = '%s'", k.c_str(), (v.size() > 40 ? (v.substr(0,40)+"...").c_str() : v.c_str()));
        } catch (...) {}
    }
    return ret;
}

static void (*orig_LogEventBT)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_LogEventBT(void* category, void* message, IL2CPP::MethodInfo* mi) {
    try {
        std::string msg = message ? ((BNM::Structures::Mono::String*)message)->str() : "";
        if (msg.find("Processing account") != std::string::npos ||
            msg.find("parse account") != std::string::npos ||
            msg.find("fetch online account") != std::string::npos ||
            msg.find("load game account") != std::string::npos ||
            msg.find("ArgumentNull") != std::string::npos) {
            std::string cat = category ? ((BNM::Structures::Mono::String*)category)->str() : "";
            // 1.31.0 base: il2cpp_raise_exception export sits at file vaddr 0xFDD86C.
            void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
            uint8_t* base = exp ? ((uint8_t*)exp - 0xFDD86CULL) : nullptr;
            BNM_FileCheckpointF("LOGEVENT-BT [%s] '%s' RVAs:%s", cat.c_str(), msg.c_str(), UnwindRVAs(base).c_str());
        }
    } catch (...) {}
    if (orig_LogEventBT) orig_LogEventBT(category, message, mi);
}
// DIAG (1.31 room load): ArgumentNullException("s") = Int32/DateTime.Parse(null)
// on some room/map field. Backtrace the ctor so the RVAs point at the game
// method doing the parse (disassemble it to find which field key it reads).
static std::string _litStr(uint64_t rva);   // defined with the gate probes
static std::atomic<int> g_anN{0};
static void (*orig_ANECtor)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_ANECtor(void* self, void* paramName, IL2CPP::MethodInfo* mi) {
    try {
        std::string pn = paramName ? ((BNM::Structures::Mono::String*)paramName)->str() : "(null)";
        if (g_anN.fetch_add(1) < 40) {
            void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
            uint8_t* base = exp ? ((uint8_t*)exp - 0xFDD86CULL) : nullptr;
            BNM_FileCheckpointF("ANE-CTOR param='%s' RVAs:%s", pn.c_str(), UnwindRVAs(base).c_str());
            if (g_anN.load() == 1) {
                // Keys read by the room-parse method around RVA 0x1c2c100; the
                // GetInt that throws is keyed by slot 0x5dbf248.
                static const uint64_t RK[] = {
                    0x5dbf248,0x5d742f0,0x5dce2f8,0x5da07b8,0x5da2ec0,0x5d7c260,0x5d72ec0,
                    0x5d78f10,0x5d78f98,0x5df93f8,0x5df1c50,0x5d8b180,0x5df96c8,0x5ddf898,
                    0x5ddb128,0x5d75108,0x5d88628,0x5dba718,0x5dfa598,0x5deef70,0x5de7e20,
                    0x5d80120,0x5db29b8,0x5d7df18,0x5dc5b78,0x5dba148,0x5da6c18,0x5dd8b10,
                    0x5dd2e28,0x5da0af8,
                };
                for (uint64_t k : RK)
                    BNM_FileCheckpointF("ROOMKEY %llx '%s'", (unsigned long long)k, _litStr(k).c_str());
            }
        }
    } catch (...) {}
    if (orig_ANECtor) orig_ANECtor(self, paramName, mi);
}
static void InstallANECtor() {
    try {
        Class c("System", "ArgumentNullException", Image("mscorlib.dll"));
        if (!c.IsValid()) { BNM_FileCheckpoint("ANE: class NOT FOUND"); return; }
        MethodBase m = c.GetMethod(".ctor", 1);
        if (m.IsValid()) { BasicHook(m, hook_ANECtor, orig_ANECtor); BNM_FileCheckpoint("ANE: .ctor(1) hooked"); }
        else BNM_FileCheckpoint("ANE: .ctor(1) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("ANE: EXCEPTION"); }
}
static void InstallLogEventBT() {
    try {
        Class lm("", "LogManager", Image("Assembly-CSharp.dll"));
        if (!lm.IsValid()) { BNM_FileCheckpoint("LogEventBT: LogManager NOT FOUND"); return; }
        MethodBase m = lm.GetMethod("LogEvent", 2);
        if (m.IsValid()) { BasicHook(m, hook_LogEventBT, orig_LogEventBT); BNM_FileCheckpoint("LogEventBT: LogEvent hooked"); }
        else BNM_FileCheckpoint("LogEventBT: LogEvent(2) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("LogEventBT: EXCEPTION"); }
}

// DIAG: probe the account-process method at RVA 0x287FD94 (it logs "Processing
// account data" then gates success on `[arg1] & 1`). Log the class of each arg
// (and the deciding byte) so we learn what the gate actually tests.
static bool _plausiblePtr(void* p) {
    uintptr_t v = (uintptr_t)p;
    return p && (v & 7) == 0 && v > 0x10000 && v < 0x8000000000ULL;
}
static std::atomic<int> g_apN{0};
static void* (*orig_AP)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static void* hook_AP(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    if (g_apN.fetch_add(1) < 6) {
        try {
            typedef const char* (*cn_t)(void*);
            typedef void* (*oc_t)(void*);
            static auto gn = (cn_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_get_name");
            static auto gc = (oc_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_get_class");
            const char* c0 = "?"; const char* c1 = "?";
            if (gn && gc) {
                if (_plausiblePtr(a0) && _plausiblePtr(*(void**)a0)) c0 = gn(gc(a0));
                if (_plausiblePtr(a1) && _plausiblePtr(*(void**)a1)) c1 = gn(gc(a1));
            }
            int b1 = _plausiblePtr(a1) ? (*(volatile uint8_t*)a1 & 1) : -1;
            BNM_FileCheckpointF("ACCTPROC a0cls=%s a1cls=%s a1bit0=%d a0=%p a1=%p a2=%p",
                                c0, c1, b1, a0, a1, a2);
        } catch (...) {}
    }
    g_inCredProc.store(true);
    void* r = orig_AP ? orig_AP(a0, a1, a2, a3, a4, a5, a6, a7) : nullptr;
    g_inCredProc.store(false);
    return r;
}
static void InstallAcctProbe() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("[ap] no il2cpp base"); return; }
    void* fn = (uint8_t*)exp - 0xFDD86CULL + 0x287FD94ULL;
    void* tr = nullptr;
    if (DobbyHook(fn, (dobby_dummy_func_t)hook_AP, (dobby_dummy_func_t*)&tr) != 0) {
        BNM_FileCheckpointF("[ap] hook FAILED @ %p", fn); return;
    }
    orig_AP = (void*(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    BNM_FileCheckpointF("[ap] acct-process @ %p hooked", fn);
}

// DIAG: log the KEY (x1) + bool result of each field-read helper used by the
// account parse (RVAs 0x435BB20 / 0x435B538 / 0x435A0D8). The sequence of keys
// (and where it stops / which returns false) pinpoints the failing field.
// Robust il2cpp System.String decode: +0x10 int32 length, +0x14 UTF-16 chars.
// Avoids BNM str() (which was returning "?" here) and never throws.
static std::string _monoStr(void* o) {
    if (!_plausiblePtr(o)) return "(badptr)";
    int32_t len = *(volatile int32_t*)((uint8_t*)o + 0x10);
    if (len < 0 || len > 1024) return std::string("(len=") + std::to_string(len) + ")";
    const volatile uint16_t* cs = (const volatile uint16_t*)((uint8_t*)o + 0x14);
    std::string s;
    for (int i = 0; i < len; i++) {
        uint16_t c = cs[i];
        s += (c >= 32 && c < 127) ? (char)c : '.';
    }
    return s;
}
static std::string _hex(void* p, int n) {
    std::string s;
    if (!_plausiblePtr(p)) return "(bad)";
    char b[4];
    for (int i = 0; i < n; i++) { snprintf(b, 4, "%02x", ((volatile uint8_t*)p)[i]); s += b; if ((i&7)==7) s += ' '; }
    return s;
}
// The two fatal gates: if either returns false, the parse reports failure.
// RVA 0x257570C (gate A) and 0x256C0C8 (gate B). Log result + arg struct bytes.
static uintptr_t g_il2cppBase = 0;   // set in InstallFieldReadProbe
#define _retOff() ((uintptr_t)__builtin_return_address(0) - g_il2cppBase)
// Read an il2cpp string-literal referenced as `ldr x8,[base+rva]; ldr x1,[x8]`
// i.e. String* = *(*(base+rva)). Used to decode the obfuscated field-name keys.
static std::string _litStr(uint64_t rva) {
    if (!g_il2cppBase) return "(nobase)";
    void** slot = (void**)(g_il2cppBase + rva);
    if (!_plausiblePtr(slot)) return "(badslot)";
    void* entry = *slot;
    if (!_plausiblePtr(entry)) return "(badentry)";
    void* s = *(void**)entry;
    return _monoStr(s);
}
static std::atomic<int> g_gA{0}, g_gB{0};
static uint64_t (*orig_GA)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_GA(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_GA ? orig_GA(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    int n = g_gA.fetch_add(1);
    if (n < 60)
        BNM_FileCheckpointF("GATE-A(257570c) r=%d a0=[%s] a2=[%s]", (int)(r&1), _hex(a0,16).c_str(), _hex(a2,16).c_str());
    if (n == 0) {
        // One-shot: decode EVERY field-name string literal Gate A references
        // (all `ldr x1,[xN]` key slots extracted from the disassembly), so we
        // get the complete set of account fields the parser reads in one run.
        static const uint64_t KSLOTS[] = {
            0x5dd5d98,0x5daf478,0x5e001a8,0x5dd0bd0,0x5dac638,0x5d7ac00,0x5d78af0,0x5dea088,
            0x5dc6ea0,0x5d85bb0,0x5da2168,0x5ddc1b0,0x5df6e78,0x5dcaaa0,0x5dcf420,0x5e06770,
            0x5dcb628,0x5de4700,0x5dcef20,0x5d854a0,0x5dd3070,0x5dd5c30,0x5dbfe50,0x5d95990,
            0x5d8b7a8,0x5da3a60,0x5d806c8,0x5da5260,0x5d84d30,0x5db1398,0x5dbe210,0x5dcba20,
            0x5dc37e0,0x5dba538,0x5d731f0,0x5de4b00,0x5d73af0,0x5e04968,0x5d89cc0,0x5dc0588,
            0x5d720a0,0x5e06a80,0x5db8e20,0x5dbc2e0,0x5d93a18,0x5da9ad0,0x5daf998,0x5daba90,
            0x5dd7590,
        };
        for (uint64_t s : KSLOTS)
            BNM_FileCheckpointF("GAFIELD %llx '%s'", (unsigned long long)s, _litStr(s).c_str());
    }
    return r;
}
static std::atomic<int> g_gBfield{0};
static uint64_t (*orig_GB)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_GB(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_GB ? orig_GB(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    if (g_gB.fetch_add(1) < 60)
        BNM_FileCheckpointF("GATE-B(256c0c8) r=%d a0=[%s]", (int)(r&1), _hex(a0,24).c_str());
    if (g_gBfield.fetch_add(1) == 0) {
        // One-shot: decode the field-name literals Gate B (the validator) reads.
        static const uint64_t GBS[] = {
            0x5d806c8,0x5da5260,0x5d84d30,0x5db2530,0x5e02b08,0x5d89cc0,
            0x5dc0588,0x5d720a0,0x5e04968,0x5e06a80,0x5db8e20,
        };
        for (uint64_t s : GBS)
            BNM_FileCheckpointF("GBFIELD %llx '%s'", (unsigned long long)s, _litStr(s).c_str());
    }
    // Gate B is the client-side integrity/tamper validation of the account data.
    // It hash-validates fields we legitimately modify on our private server, so
    // it fails. Force it to pass, but ONLY during the account-process coroutine
    // (g_inCredProc) so any other callers get the real result. orig_GB still runs
    // first (side effects preserved); we only override the boolean.
    if (g_inCredProc.load() && (r & 1) == 0) {
        BNM_FileCheckpointF("GATE-B FORCED pass (was %d)", (int)(r & 1));
        return 1;
    }
    return r;
}
// IsNullOrEmpty (RVA 0x3CDA1D0). Log value+result but ONLY for calls made from
// inside Gate B (the validator, 0x256c0c8..0x256cc00) -> pinpoints which string
// check fails validation.
static std::atomic<int> g_ineN{0};
static uint64_t (*orig_INE)(void*, void*) = nullptr;
static uint64_t hook_INE(void* a0, void* a1) {
    uintptr_t off = _retOff();
    uint64_t r = orig_INE ? orig_INE(a0, a1) : 0;
    if (g_inCredProc.load() && off >= 0x256c0c8 && off <= 0x256cc00 && g_ineN.fetch_add(1) < 60) {
        std::string v = a0 ? _monoStr(a0) : std::string("(null)");
        BNM_FileCheckpointF("GBNULL off=%llx '%s' -> %d", (unsigned long long)off, v.c_str(), (int)(r & 1));
    }
    return r;
}
// Gate A (0x257570c) requires 5 keys via Dictionary.ContainsKey (0x43554f0):
//   bool ContainsKey(this dict=x0, string key=x1, MethodInfo*=x2)
// Log key+result during cred-proc; the first key that returns 0 is the field our
// server is missing (which is what makes Gate A return false -> parse failure).
static std::atomic<int> g_ckN{0};
static uint64_t (*orig_CK)(void*, void*, void*) = nullptr;
static uint64_t hook_CK(void* a0, void* a1, void* a2) {
    uint64_t r = orig_CK ? orig_CK(a0, a1, a2) : 0;
    // Only log ContainsKey calls made from inside Gate A (0x257570c): its 5
    // required-key checks have return addrs in base+[0x2575a50 .. 0x2575ac4].
    uintptr_t ret = (uintptr_t)__builtin_return_address(0);
    if (g_il2cppBase && g_inCredProc.load()) {
        uintptr_t off = ret - g_il2cppBase;
        if (off >= 0x2575a40 && off <= 0x2575ad0) {
            std::string v = a1 ? _monoStr(a1) : std::string("(null)");
            BNM_FileCheckpointF("GAKEY off=%llx '%s' -> %d",
                                (unsigned long long)off, v.c_str(), (int)(r & 1));
        } else if (g_ckN.fetch_add(1) < 40) {
            // fallback diag: show raw return offset + key so we can locate Gate A's
            // real call offsets if the range above misses (Dobby trampoline shift).
            std::string v = a1 ? _monoStr(a1) : std::string("(null)");
            BNM_FileCheckpointF("CKANY off=%llx '%s' -> %d",
                                (unsigned long long)off, v.c_str(), (int)(r & 1));
        }
    }
    return r;
}
// ---- Gate A value-validation getters -------------------------------------
// Gate A returns false if any required typed read fails. Data-dependent fail
// exits found by disasm: GetString (0x4356550) -> null@0x2576100; TryGetValue
// (0x4355440) -> false@0x257604c; a get (0x4355298) -> null@0x2575ccc; list get
// (0x435d128). Log each call made from INSIDE Gate A (return addr in Gate A's
// body) with its key + result; the LAST line before "GATE-A r=0" is the field
// whose value our server response has in the wrong shape/type.
static std::atomic<int> g_gN{0};
static void* (*orig_GS)(void*, void*, void*, void*) = nullptr;   // GetString 0x4356550
static void* hook_GS(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    void* r = orig_GS ? orig_GS(a0, a1, a2, a3) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GS off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_GSn)(void*, void*, void*) = nullptr;         // GetString? 0x435b538
static void* hook_GSn(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_GSn ? orig_GSn(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GSn off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static uint64_t (*orig_TGV)(void*, void*, void*, void*) = nullptr; // TryGetValue 0x4355440
static uint64_t hook_TGV(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    uint64_t r = orig_TGV ? orig_TGV(a0, a1, a2, a3) : 0;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("TGV off=%llx '%s' -> %d", (unsigned long long)off, k.c_str(), (int)(r & 1));
    }
    return r;
}
static void* (*orig_GL)(void*, void*, void*, void*) = nullptr;   // list/array get 0x435d128
static void* hook_GL(void* a0, void* a1, void* a2, void* a3) {
    uintptr_t off = _retOff();
    void* r = orig_GL ? orig_GL(a0, a1, a2, a3) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("GL off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_G1)(void*, void*) = nullptr;                 // get 0x4355298 (no key)
static void* hook_G1(void* a0, void* a1) {
    uintptr_t off = _retOff();
    void* r = orig_G1 ? orig_G1(a0, a1) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80)
        BNM_FileCheckpointF("G1 off=%llx -> %s", (unsigned long long)off, r ? "ok" : "NULL");
    return r;
}
// segmentationData sub-field getters (run only after TGV('segmentationData')
// succeeds): 0x4359e88 (SA, first sub-read) and 0x435560c (SB, next three).
static void* (*orig_SA)(void*, void*, void*) = nullptr;
static void* hook_SA(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_SA ? orig_SA(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("SA off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void* (*orig_SB)(void*, void*, void*) = nullptr;
static void* hook_SB(void* a0, void* a1, void* a2) {
    uintptr_t off = _retOff();
    void* r = orig_SB ? orig_SB(a0, a1, a2) : nullptr;
    if (g_inCredProc.load() && off >= 0x2575a40 && off <= 0x25766d0 && g_gN.fetch_add(1) < 80) {
        std::string k = a1 ? _monoStr(a1) : std::string("(nullkey)");
        BNM_FileCheckpointF("SB off=%llx '%s' -> %s", (unsigned long long)off, k.c_str(), r ? "ok" : "NULL");
    }
    return r;
}
static void InstallFieldReadProbe() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) return;
    uint8_t* base = (uint8_t*)exp - 0xFDD86CULL;
    g_il2cppBase = (uintptr_t)base;
    // Only the account-parse-local gate predicates are hooked now. The hot
    // generic getters (0x4356550/0x435560c/...) were removed: inline-hooking
    // them destabilised the whole game and crashed it right as the
    // segmentationData sub-fields were read. GATE-A/GATE-B run only during the
    // account parse, so they are safe and still tell us if the parse passed.
    struct { uint64_t rva; void* hook; void** orig; } T[] = {
        {0x257570CULL, (void*)hook_GA,  (void**)&orig_GA},
        {0x256C0C8ULL, (void*)hook_GB,  (void**)&orig_GB},
        {0x3CDA1D0ULL, (void*)hook_INE, (void**)&orig_INE},
    };
    for (auto& t : T) {
        void* fn = base + t.rva; void* tr = nullptr;
        if (DobbyHook(fn, (dobby_dummy_func_t)t.hook, (dobby_dummy_func_t*)&tr) == 0) {
            *t.orig = tr; BNM_FileCheckpointF("[gate] hooked 0x%llx", (unsigned long long)t.rva);
        } else BNM_FileCheckpointF("[gate] FAILED 0x%llx", (unsigned long long)t.rva);
    }
}
// Hook the account-data string getter by RVA (il2cppBase + 0x1df5f90).
static void InstallCredGetterHook() {
    try {
        void* ans = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new_specific");
        if (!ans) { BNM_FileCheckpoint("CredGetter: no il2cpp anchor"); return; }
        uint8_t* base = (uint8_t*)ans - 0xebdc94;
        void* target = base + 0x1df5f90;
        BasicHook(target, (void*)hook_credGetter, orig_credGetter);
        BNM_FileCheckpointF("CredGetter: hooked getter @ %p", target);
    } catch (...) { BNM_FileCheckpoint("CredGetter: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  GameAnalytics kill-switch. The baked-in GameAnalytics SDK
//  (GameAnalyticsSDK.GameAnalytics, Assembly-CSharp.dll) opens a local
//  ga.sqlite3, starts a session and ships events to api.gameanalytics.com
//  (AWS us-east-1) -> player telemetry leaks to a GA account we don't own.
//  We no-op the single entry point GameAnalytics.Initialize(): with no
//  init there is no session, no DB and no network submission, and every
//  event method (NewDesignEvent/NewProgressionEvent/...) bails internally
//  on the un-initialized state, so gameplay is unaffected.
// ---------------------------------------------------------------------
static void (*orig_GA_Initialize)(IL2CPP::MethodInfo*) = nullptr;
static void hook_GA_Initialize([[maybe_unused]] IL2CPP::MethodInfo* mi) {
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true))
        BNM_FileCheckpoint("GA: Initialize() suppressed - no telemetry");
    // intentionally do nothing: GA never starts
}
static void InstallGameAnalyticsBlock() {
    try {
        Class ga("GameAnalyticsSDK", "GameAnalytics", Image("Assembly-CSharp.dll"));
        if (!ga.IsValid()) { BNM_FileCheckpoint("GA: class NOT FOUND"); return; }
        MethodBase m = ga.GetMethod("Initialize", 0);
        if (m.IsValid()) { BasicHook(m, hook_GA_Initialize, orig_GA_Initialize);
                           BNM_FileCheckpoint("GA: Initialize() hooked (telemetry disabled)"); }
        else BNM_FileCheckpoint("GA: Initialize(0) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("GA: EXCEPTION installing block"); }
}

// ---------------------------------------------------------------------
//  Clean version label. ApplicationVersionDisplayUI.Start() builds the
//  on-screen string as  "v" + "{0}({1})"  (app version + a compatibility
//  version), e.g. "v1.19.0-blaze(1.19.0)". We let Start() run, then
//  overwrite versionText (field @ 0x18) with just Application.version
//  ("1.19.0-blaze") so the label reads exactly the manifest version.
// ---------------------------------------------------------------------
static void* (*p_gchandle_new)(void*, bool) = nullptr;   // il2cpp GC handles (Remote client)
static void* g_uiFontHandle = nullptr;    // pinned UI Font (from the version label)
static void (*orig_AVDU_Start)(void*, void*) = nullptr;
static void hook_AVDU_Start(void* self, void* mi) {
    if (orig_AVDU_Start) orig_AVDU_Start(self, mi);
    try {
        if (!self) return;
        void* txt = *(void**)((uint8_t*)self + 0x18);   // Text versionText
        if (!txt) return;
        Class app("UnityEngine", "Application", Image("UnityEngine.CoreModule.dll"));
        Method<BNM::Structures::Mono::String*> gv(app.GetMethod("get_version", 0));
        if (!gv.IsValid()) return;
        auto* ver = gv();
        if (!ver) return;
        Class txtc("UnityEngine.UI", "Text", Image("UnityEngine.UI.dll"));
        // Cache the game's UI font for server announcements (Remote client).
        if (!g_uiFontHandle && p_gchandle_new) {
            Method<Il2CppObject*> gf(txtc.GetMethod("get_font", 0));
            Il2CppObject* f = gf.IsValid() ? gf[(Il2CppObject*)txt]() : nullptr;
            if (f) g_uiFontHandle = p_gchandle_new(f, false);
        }
        Method<void> st(txtc.GetMethod("set_text", 1));
        if (!st.IsValid()) return;
        st.SetInstance((IL2CPP::Il2CppObject*)txt);
        st(ver);
    } catch (...) {}
}
static void InstallVersionLabelClean() {
    try {
        Class c("", "ApplicationVersionDisplayUI", Image("Assembly-CSharp.dll"));
        if (!c.IsValid()) { BNM_FileCheckpoint("VerLabel: class NOT FOUND"); return; }
        MethodBase m = c.GetMethod("Start", 0);
        if (m.IsValid()) { BasicHook(m, hook_AVDU_Start, orig_AVDU_Start);
                           BNM_FileCheckpoint("VerLabel: Start() hooked (clean version)"); }
        else BNM_FileCheckpoint("VerLabel: Start(0) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("VerLabel: EXCEPTION"); }
}

// ---------------------------------------------------------------------
//  Backend redirect. In 1.19.0 the API base URL was plaintext in
//  global-metadata.dat and redirected by an in-place string patch. In
//  1.31.0 it is decrypted at runtime, so we instead hook the name-stable
//  framework entry points every HTTP request funnels through, LOG the full
//  (decrypted) URL for diagnosis, and rewrite the Yeeps execute-api gateway
//  host to the self-hosted server. The server strips the /xxxxxxxxxxxxxxxxxx
//  prefix (main.py strip_apk_prefix) and routes normally, exactly as the
//  1.19.0-blaze client's patched URL did.
// ---------------------------------------------------------------------
// ---------------------------------------------------------------------
//  Build identity + integrity tag. Computed once at startup from the
//  native libraries actually loaded on the headset and sent to the server
//  in the path of every API-gateway request (login etc.) as
//  /c/<kBuildId>.<fnv64(libil2cpp.so)>.<fnv64(libBNMExplorer.so)>/
//  The server records it per account and blocks wrong/old/modified builds.
//  Bump kBuildId for every APK you distribute.
// ---------------------------------------------------------------------
static const char* kBuildId = "kit1";
static std::string g_buildTag = std::string(kBuildId) + ".pending.pending";

static uint64_t Fnv1aFile(const char* path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return 0;
    uint64_t h = 1469598103934665603ULL;
    std::vector<uint8_t> buf(1 << 20);
    ssize_t n;
    while ((n = read(fd, buf.data(), buf.size())) > 0)
        for (ssize_t i = 0; i < n; i++) { h ^= buf[i]; h *= 1099511628211ULL; }
    close(fd);
    return h;
}
struct LibPaths { std::string il2cpp, bnm; };
static int FindLibsCb(struct dl_phdr_info* info, size_t, void* data) {
    auto* lp = (LibPaths*)data;
    if (!info->dlpi_name) return 0;
    std::string n = info->dlpi_name;
    if (n.size() >= 12 && n.compare(n.size() - 12, 12, "libil2cpp.so") == 0) lp->il2cpp = n;
    if (n.size() >= 17 && n.compare(n.size() - 17, 17, "libBNMExplorer.so") == 0) lp->bnm = n;
    return 0;
}
static void ComputeBuildTag() {
    LibPaths lp;
    dl_iterate_phdr(FindLibsCb, &lp);
    char tag[96];
    snprintf(tag, sizeof(tag), "%s.%016llx.%016llx", kBuildId,
             (unsigned long long)(lp.il2cpp.empty() ? 0 : Fnv1aFile(lp.il2cpp.c_str())),
             (unsigned long long)(lp.bnm.empty() ? 0 : Fnv1aFile(lp.bnm.c_str())));
    g_buildTag = tag;
    BNM_FileCheckpointF("BuildTag: %s", tag);
}

// ---------------------------------------------------------------------
//  Headset device key (131b14+). The game API has no player login of its
//  own, so every API URL carries /k/<key>/ and the server ties each account
//  to the headset that owns it (vr-server services/device_auth.py).
//  key = sha256("blaze-device-v1|" + Android ID): ANDROID_ID is per device +
//  per signing key, survives reinstalls, and is never sent raw. If it can't
//  be read, a random key is kept in the app's files dir instead.
// ---------------------------------------------------------------------
static JavaVM* g_vm = nullptr;
static std::string g_deviceKey;

namespace sha256mini {
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
static std::string hex(const std::string& msg) {
    uint32_t h[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    std::vector<uint8_t> m(msg.begin(), msg.end());
    uint64_t bits = (uint64_t)m.size() * 8;
    m.push_back(0x80);
    while (m.size() % 64 != 56) m.push_back(0);
    for (int i = 7; i >= 0; i--) m.push_back((uint8_t)(bits >> (i * 8)));
    for (size_t off = 0; off < m.size(); off += 64) {
        uint32_t w[64];
        for (int i = 0; i < 16; i++)
            w[i] = (uint32_t)m[off + 4*i] << 24 | (uint32_t)m[off + 4*i + 1] << 16 |
                   (uint32_t)m[off + 4*i + 2] << 8 | (uint32_t)m[off + 4*i + 3];
        for (int i = 16; i < 64; i++) {
            uint32_t s0 = rotr(w[i-15], 7) ^ rotr(w[i-15], 18) ^ (w[i-15] >> 3);
            uint32_t s1 = rotr(w[i-2], 17) ^ rotr(w[i-2], 19) ^ (w[i-2] >> 10);
            w[i] = w[i-16] + s0 + w[i-7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; i++) {
            uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = hh + S1 + ch + K[i] + w[i];
            uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }
    char out[65];
    for (int i = 0; i < 8; i++) snprintf(out + i * 8, 9, "%08x", h[i]);
    return std::string(out, 64);
}
}  // namespace sha256mini

static std::string ReadAndroidId() {
    if (!g_vm) return "";
    JNIEnv* env = nullptr;
    bool attached = false;
    if (g_vm->GetEnv((void**)&env, JNI_VERSION_1_6) != JNI_OK) {
        if (g_vm->AttachCurrentThread(&env, nullptr) != JNI_OK) return "";
        attached = true;
    }
    std::string id;
    auto clear = [&]() { if (env->ExceptionCheck()) { env->ExceptionClear(); return true; } return false; };
    do {
        jclass at = env->FindClass("android/app/ActivityThread");
        if (clear() || !at) break;
        jmethodID cur = env->GetStaticMethodID(at, "currentApplication", "()Landroid/app/Application;");
        if (clear() || !cur) break;
        jobject app = env->CallStaticObjectMethod(at, cur);
        if (clear() || !app) break;
        jclass ctx = env->FindClass("android/content/Context");
        if (clear() || !ctx) break;
        jmethodID gcr = env->GetMethodID(ctx, "getContentResolver", "()Landroid/content/ContentResolver;");
        if (clear() || !gcr) break;
        jobject cr = env->CallObjectMethod(app, gcr);
        if (clear() || !cr) break;
        jclass sec = env->FindClass("android/provider/Settings$Secure");
        if (clear() || !sec) break;
        jmethodID gs = env->GetStaticMethodID(sec, "getString",
                           "(Landroid/content/ContentResolver;Ljava/lang/String;)Ljava/lang/String;");
        if (clear() || !gs) break;
        jstring name = env->NewStringUTF("android_id");
        auto js = (jstring)env->CallStaticObjectMethod(sec, gs, cr, name);
        if (clear() || !js) break;
        const char* c = env->GetStringUTFChars(js, nullptr);
        if (c) { id = c; env->ReleaseStringUTFChars(js, c); }
    } while (false);
    if (attached) g_vm->DetachCurrentThread();
    return id;
}

static std::string RandomKeyFile() {
    // fallback: a random key kept in /data/data/<package>/files
    char pkg[128] = {0};
    int fd = open("/proc/self/cmdline", O_RDONLY);
    if (fd >= 0) { read(fd, pkg, sizeof(pkg) - 1); close(fd); }
    std::string path = std::string("/data/data/") + pkg + "/files/blaze_device_key";
    std::string key;
    fd = open(path.c_str(), O_RDONLY);
    if (fd >= 0) {
        char buf[65] = {0};
        if (read(fd, buf, 64) == 64) key.assign(buf, 64);
        close(fd);
    }
    if (key.size() == 64) return key;
    uint8_t rnd[32] = {0};
    fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) { read(fd, rnd, sizeof(rnd)); close(fd); }
    key = sha256mini::hex(std::string((const char*)rnd, sizeof(rnd)));
    fd = open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd >= 0) { write(fd, key.data(), key.size()); close(fd); }
    return key;
}

// =====================================================================
//  Self-host kit settings (Yeeps-1.31.0-blaze kit). Read once at load:
//   1) the settings file inside the APK: lib/arm64-v8a/libblazeconfig.so (a
//      plain text file; Android extracts lib*.so files next to this library,
//      so no asset API is needed). tools/configure_apk.py writes it.
//   2) optional override on the headset (handy for testing):
//      /sdcard/Android/data/com.TrassGames.Yeeps/files/blaze-config.txt
//  Lines are key=value; '#' starts a comment. Keys:
//   server               base URL of the host's server, e.g. https://x.trycloudflare.com
//                        or http://192.168.1.20:8000 (plain HTTP is allowed)
//   photon_app_id        the host's Photon PUN (Realtime) app id
//   photon_voice_app_id  the host's Photon Voice app id
//   photon_region        optional fixed region (us, usw, eu, ...); blank keeps the APK's
//   discord              link the news-board Discord button opens
// =====================================================================
static std::map<std::string, std::string> g_kitCfg;
static std::string KitCfg(const char* key, const char* def = "") {
    auto it = g_kitCfg.find(key);
    return (it == g_kitCfg.end() || it->second.empty()) ? std::string(def) : it->second;
}
static std::string KitTrim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t"), b = s.find_last_not_of(" \t");
    return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}
static void ParseKitCfg(const std::string& text) {
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t h = line.find('#');
        if (h != std::string::npos) line = line.substr(0, h);
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        std::string k = KitTrim(line.substr(0, eq));
        if (!k.empty()) g_kitCfg[k] = KitTrim(line.substr(eq + 1));
    }
}
static bool ReadSmallFile(const std::string& path, std::string& out) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[4096];
    size_t n;
    out.clear();
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0 && out.size() < 65536) out.append(buf, n);
    fclose(f);
    return true;
}
static void LoadKitConfig() {
    std::string text, from = "none";
    Dl_info info{};
    if (dladdr((void*)&LoadKitConfig, &info) && info.dli_fname) {
        std::string dir(info.dli_fname);
        size_t cut = dir.rfind('/');
        if (cut != std::string::npos && ReadSmallFile(dir.substr(0, cut) + "/libblazeconfig.so", text)) {
            ParseKitCfg(text);
            from = "apk";
        }
    }
    if (ReadSmallFile("/sdcard/Android/data/com.TrassGames.Yeeps/files/blaze-config.txt", text)) {
        ParseKitCfg(text);
        from += "+headset";
    }
    std::string srv = KitCfg("server");
    while (!srv.empty() && srv.back() == '/') srv.pop_back();
    g_kitCfg["server"] = srv;
    BNM_FileCheckpointF("Kit: settings from %s, server=%s photon=%s voice=%s region=%s", from.c_str(),
                        srv.empty() ? "(none)" : srv.c_str(),
                        KitCfg("photon_app_id").empty() ? "(none)" : "set",
                        KitCfg("photon_voice_app_id").empty() ? "(none)" : "set",
                        KitCfg("photon_region", "(apk default)").c_str());
}
static std::string KitServer() { return KitCfg("server", "http://127.0.0.1:8000"); }
static std::string KitUrl(const char* path) { return KitServer() + path; }

static void ComputeDeviceKey() {
    std::string aid = ReadAndroidId();
    if (!aid.empty()) {
        g_deviceKey = sha256mini::hex("blaze-device-v1|" + aid);
        BNM_FileCheckpoint("DeviceKey: from android id");
    } else {
        g_deviceKey = RandomKeyFile();
        BNM_FileCheckpoint("DeviceKey: random (android id unavailable)");
    }
}

// The kit server strips this fixed path prefix (main.py _PATCH_PREFIX).
static const char* kSelfPrefix = "/xxxxxxxxxxxxxxxxxx";
static bool RewriteBackendUrl(const std::string& in, std::string& out) {
    // Yeeps uses THREE AWS backends, all of which must go to our proxy:
    //   *.execute-api.*.amazonaws.com  -> API Gateway (login/account)
    //   lambda.*.amazonaws.com         -> direct Lambda g2_* calls (room/game data)
    //   dynamodb.*.amazonaws.com       -> direct DynamoDB (room map nodes, etc.)
    // Asset CDNs (s3/cloudfront) are left alone so real game assets still load.
    auto sch = in.find("://");
    if (sch == std::string::npos) return false;
    size_t hostStart = sch + 3;
    size_t pathStart = in.find('/', hostStart);
    std::string host = (pathStart == std::string::npos)
                     ? in.substr(hostStart) : in.substr(hostStart, pathStart - hostStart);
    std::string path = (pathStart == std::string::npos) ? "/" : in.substr(pathStart);
    bool aws = host.find(".amazonaws.com") != std::string::npos;
    if (!aws) return false;
    bool match = (host.find(".execute-api.") != std::string::npos)
              || (host.rfind("lambda.", 0) == 0)
              || (host.rfind("dynamodb.", 0) == 0);
    if (!match) return false;
    // API-gateway URLs (login etc.) are rewritten whole, so they carry the
    // build tag. Lambda/DynamoDB are host-only SDK endpoints the SDK builds
    // paths onto; leave those untagged.
    std::string base = KitServer() + kSelfPrefix;
    if (g_deviceKey.size() == 64) base += "/k/" + g_deviceKey;   // who's asking (device_auth.py)
    if (host.find(".execute-api.") != std::string::npos)
        out = base + "/c/" + g_buildTag + path;
    else
        out = base + path;   // keep path + query
    return true;
}
static void* MaybeRewrite(void* urlObj, [[maybe_unused]] const char* tag) {
    try {
        if (!urlObj) return urlObj;
        std::string u = ((BNM::Structures::Mono::String*)urlObj)->str();
        std::string nu;
        if (RewriteBackendUrl(u, nu))
            return CreateMonoString(nu.c_str());
    } catch (...) {}
    return urlObj;
}
static void (*orig_UWR_seturl)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_UWR_seturl(void* self, void* url, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(url, "UWR.set_url");
    if (orig_UWR_seturl) orig_UWR_seturl(self, pass, mi);
}
static void (*orig_Uri_ctor)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void hook_Uri_ctor(void* self, void* uriStr, IL2CPP::MethodInfo* mi) {
    void* pass = MaybeRewrite(uriStr, "Uri.ctor");
    if (orig_Uri_ctor) orig_Uri_ctor(self, pass, mi);
}
// ---------------------------------------------------------------------
//  Photon room joins carry the headset key too (131b15+). Photon Cloud
//  calls our custom-auth URL with the client's AuthGetParameters, so we add
//  dk=<device key> right before the client authenticates; the server then
//  refuses a join that claims someone else's account (device_auth.py).
// ---------------------------------------------------------------------
static void AddDeviceKeyToAuth(void* authValues) {
    try {
        if (!authValues || g_deviceKey.size() != 64) return;
        Class av("Photon.Realtime", "AuthenticationValues");
        Method<BNM::Structures::Mono::String*> getP(av.GetMethod("get_AuthGetParameters", 0));
        Method<void> setP(av.GetMethod("set_AuthGetParameters", 1));
        if (!getP.IsValid() || !setP.IsValid()) return;
        auto* cur = getP[(Il2CppObject*)authValues]();
        std::string q = cur ? cur->str() : "";
        if (q.find("dk=") != std::string::npos) return;
        if (!q.empty()) q += "&";
        q += "dk=" + g_deviceKey;
        setP[(Il2CppObject*)authValues](CreateMonoString(q.c_str()));
    } catch (...) {}
}
static bool (*orig_OpAuth)(void*, void*, void*, void*, void*, bool, IL2CPP::MethodInfo*) = nullptr;
static bool hook_OpAuth(void* self, void* appId, void* appVer, void* av, void* region, bool stats, IL2CPP::MethodInfo* mi) {
    AddDeviceKeyToAuth(av);
    return orig_OpAuth ? orig_OpAuth(self, appId, appVer, av, region, stats, mi) : false;
}
static bool (*orig_OpAuthOnce)(void*, void*, void*, void*, void*, int32_t, int32_t, IL2CPP::MethodInfo*) = nullptr;
static bool hook_OpAuthOnce(void* self, void* appId, void* appVer, void* av, void* region, int32_t enc, int32_t proto, IL2CPP::MethodInfo* mi) {
    AddDeviceKeyToAuth(av);
    return orig_OpAuthOnce ? orig_OpAuthOnce(self, appId, appVer, av, region, enc, proto, mi) : false;
}
static void InstallPhotonAuthKey() {
    try {
        Class lbp("Photon.Realtime", "LoadBalancingPeer");
        if (!lbp.IsValid()) { BNM_FileCheckpoint("[auth] LoadBalancingPeer NOT FOUND"); return; }
        MethodBase a = lbp.GetMethod("OpAuthenticate", 5);
        if (a.IsValid()) { BasicHook(a, hook_OpAuth, orig_OpAuth); BNM_FileCheckpoint("[auth] OpAuthenticate hooked"); }
        else BNM_FileCheckpoint("[auth] OpAuthenticate(5) NOT FOUND");
        MethodBase b = lbp.GetMethod("OpAuthenticateOnce", 6);
        if (b.IsValid()) { BasicHook(b, hook_OpAuthOnce, orig_OpAuthOnce); BNM_FileCheckpoint("[auth] OpAuthenticateOnce hooked"); }
        else BNM_FileCheckpoint("[auth] OpAuthenticateOnce(6) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[auth] EXC"); }
}

static void InstallBackendRedirect() {
    try {
        Class uwr("UnityEngine.Networking", "UnityWebRequest");
        if (uwr.IsValid()) {
            MethodBase m = uwr.GetMethod("set_url", 1);
            if (m.IsValid()) { BasicHook(m, hook_UWR_seturl, orig_UWR_seturl);
                               BNM_FileCheckpoint("[net] UnityWebRequest.set_url hooked"); }
            else BNM_FileCheckpoint("[net] UWR.set_url(1) NOT FOUND");
        } else BNM_FileCheckpoint("[net] UnityWebRequest class NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[net] EXC UWR"); }
    try {
        Class uri("System", "Uri");
        if (uri.IsValid()) {
            MethodBase m = uri.GetMethod(".ctor", 1);
            if (m.IsValid()) { BasicHook(m, hook_Uri_ctor, orig_Uri_ctor);
                               BNM_FileCheckpoint("[net] System.Uri.ctor(1) hooked"); }
            else BNM_FileCheckpoint("[net] Uri.ctor(1) NOT FOUND");
        } else BNM_FileCheckpoint("[net] System.Uri class NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[net] EXC Uri"); }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: LitJson ReadValue trace. The 1.31 account parse uses
//  ThirdParty.Json.LitJson.JsonMapper (AWSSDK.Core.dll). ReadValue(Type,
//  JsonReader) is the recursive workhorse that converts each JSON value to
//  the target member type and throws on a type mismatch. We log the target
//  type FullName of every ReadValue to logcat (tag JSONRV); the LAST one
//  logged right before "Failed to parse account data" is the field whose
//  value type the 1.19-shape server response doesn't satisfy for 1.31.
// ---------------------------------------------------------------------
static std::atomic<int> g_rvN{0};
static void* (*orig_JM_ReadValue)(void*, void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_JM_ReadValue(void* typeObj, void* reader, IL2CPP::MethodInfo* mi) {
    int n = ++g_rvN;
    try {
        if (typeObj) {
            Method<BNM::Structures::Mono::String*> gfn(Class("System","Type").GetMethod("get_FullName", 0));
            if (gfn.IsValid()) {
                auto* s = gfn[(IL2CPP::Il2CppObject*)typeObj]();
                __android_log_print(ANDROID_LOG_ERROR, "JSONRV", "RV#%d %s", n, s ? s->str().c_str() : "?");
            }
        }
    } catch (...) {}
    return orig_JM_ReadValue ? orig_JM_ReadValue(typeObj, reader, mi) : nullptr;
}
static void InstallReadValueTrace() {
    try {
        Class jm("ThirdParty.Json.LitJson", "JsonMapper", Image("AWSSDK.Core.dll"));
        if (!jm.IsValid()) { BNM_FileCheckpoint("[json] JsonMapper class NOT FOUND"); return; }
        MethodBase m = jm.GetMethod("ReadValue", 2);
        if (m.IsValid()) { BasicHook(m, hook_JM_ReadValue, orig_JM_ReadValue);
                           BNM_FileCheckpoint("[json] JsonMapper.ReadValue(2) traced"); }
        else BNM_FileCheckpoint("[json] ReadValue(2) NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[json] EXC ReadValue"); }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: trace string->number/bool Parse(String). The 1.19-shape
//  server response carries many empty-string fields (banExpireTimestamp,
//  oculusNumber, loginStreakPartition, timestamps). If 1.31 parses one of
//  these as a number, Parse("") throws FormatException -> the swallowed
//  "Failed to parse account data". We log every Parse arg (tag PARSE); the
//  LAST value before the fatal pinpoints the failing field's value.
// ---------------------------------------------------------------------
static int32_t (*orig_i32Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static int32_t hook_i32Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Int32 '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_i32Parse ? orig_i32Parse(s, mi) : 0;
}
static int64_t (*orig_i64Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static int64_t hook_i64Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Int64 '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_i64Parse ? orig_i64Parse(s, mi) : 0;
}
static float (*orig_f32Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static float hook_f32Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Single '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_f32Parse ? orig_f32Parse(s, mi) : 0;
}
static double (*orig_f64Parse)(void*, IL2CPP::MethodInfo*) = nullptr;
static double hook_f64Parse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Double '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_f64Parse ? orig_f64Parse(s, mi) : 0;
}
static bool (*orig_boolParse)(void*, IL2CPP::MethodInfo*) = nullptr;
static bool hook_boolParse(void* s, IL2CPP::MethodInfo* mi) {
    try { __android_log_print(ANDROID_LOG_ERROR, "PARSE", "Bool '%s'", s ? ((BNM::Structures::Mono::String*)s)->str().c_str() : "(null)"); } catch(...){}
    return orig_boolParse ? orig_boolParse(s, mi) : false;
}
static void InstallParseTrace() {
    struct { const char* ns; const char* cls; void* hook; void** orig; } T[] = {
        {"System","Int32",  (void*)hook_i32Parse,  (void**)&orig_i32Parse},
        {"System","Int64",  (void*)hook_i64Parse,  (void**)&orig_i64Parse},
        {"System","Single", (void*)hook_f32Parse,  (void**)&orig_f32Parse},
        {"System","Double", (void*)hook_f64Parse,  (void**)&orig_f64Parse},
        {"System","Boolean",(void*)hook_boolParse, (void**)&orig_boolParse},
    };
    for (auto& t : T) {
        try {
            Class c(t.ns, t.cls);
            if (!c.IsValid()) { BNM_FileCheckpointF("[parse] %s class NOT FOUND", t.cls); continue; }
            MethodBase m = c.GetMethod("Parse", 1);
            if (m.IsValid()) { BasicHook(m, t.hook, *t.orig);
                               BNM_FileCheckpointF("[parse] %s.Parse(1) hooked", t.cls); }
            else BNM_FileCheckpointF("[parse] %s.Parse(1) NOT FOUND", t.cls);
        } catch (...) { BNM_FileCheckpointF("[parse] EXC %s", t.cls); }
    }
}

// ---------------------------------------------------------------------
//  DIAGNOSTIC: intercept il2cpp_raise_exception. Parser-agnostic — catches
//  the real exception (type + message) the account parse throws before the
//  game swallows it into "Failed to parse account data". The RAISE line
//  logged right before the fatal names the failing type/value (e.g. an
//  InvalidCastException or FormatException on a specific field value).
// ---------------------------------------------------------------------
// The public export il2cpp_raise_exception (file vaddr 0xFDD86C in this 1.31.0
// libil2cpp.so) is a thunk that tail-calls the INTERNAL throw routine at vaddr
// 0xFBDFFC — the function managed `throw` statements actually call. Managed
// exceptions bypass the export, so we hook the internal routine instead,
// addressing it relative to the export's resolved runtime address.
static constexpr uintptr_t IL2CPP_RAISE_EXPORT_VADDR   = 0xFDD86CULL;
static constexpr uintptr_t IL2CPP_RAISE_INTERNAL_VADDR = 0xFBDFFCULL;
static thread_local bool g_inRaise = false;
static void (*orig_internal_raise)(void*, void*) = nullptr;
static void hook_internal_raise(void* ex, void* a1) {
    if (!g_inRaise) {
        g_inRaise = true;
        try {
            if (ex) {
                typedef const char* (*clsname_t)(void*);
                typedef void* (*objclass_t)(void*);
                static auto getName  = (clsname_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_get_name");
                static auto getClass = (objclass_t)dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_get_class");
                const char* cn = (getName && getClass) ? getName(getClass(ex)) : nullptr;
                std::string msg = "?";
                try {
                    auto* o = (IL2CPP::Il2CppObject*)ex;
                    Class ec(o->klass);
                    if (ec.IsValid()) {
                        auto gm = ec.GetMethod("get_Message", 0);
                        if (gm.IsValid()) { Method<BNM::Structures::Mono::String*> m(gm); auto* s = m[o](); if (s) msg = s->str(); }
                    }
                } catch (...) {}
                __android_log_print(ANDROID_LOG_ERROR, "RAISE", "%s: %s", cn ? cn : "?", msg.c_str());
            }
        } catch (...) {}
        g_inRaise = false;   // reset BEFORE orig: Raise unwinds and never returns
    }
    if (orig_internal_raise) orig_internal_raise(ex, a1);
}
static void InstallRaiseTrace() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("[raise] export NOT FOUND"); return; }
    uintptr_t base = (uintptr_t)exp - IL2CPP_RAISE_EXPORT_VADDR;
    void* internalRaise = (void*)(base + IL2CPP_RAISE_INTERNAL_VADDR);
    void* tramp = nullptr;
    if (DobbyHook(internalRaise, (dobby_dummy_func_t)hook_internal_raise, (dobby_dummy_func_t*)&tramp) != 0) {
        BNM_FileCheckpointF("[raise] DobbyHook FAILED @ %p", internalRaise); return;
    }
    orig_internal_raise = (void(*)(void*, void*))tramp;
    BNM_FileCheckpointF("[raise] internal Raise hooked @ %p (base=%p)", internalRaise, (void*)base);
}

// ---------------------------------------------------------------------
//  CAPTURE: dump large HTTP response bodies (the account JSON is ~30KB) to a
//  file, so the REAL 1.31 backend's questLogIntoAccount response can be read
//  off-device and diffed against the self-hosted server's response.
// ---------------------------------------------------------------------
static void DumpRespFile(const std::string& s) {
    FILE* f = fopen("/sdcard/Android/data/com.TrassGames.Yeeps/files/resp_dump.txt", "a");
    if (!f) f = fopen("/data/local/tmp/resp_dump.txt", "a");
    if (f) { fprintf(f, "\n===RESP len=%zu===\n%s\n", s.size(), s.c_str()); fclose(f); }
}
static void* (*orig_DH_get_text)(void*, IL2CPP::MethodInfo*) = nullptr;
static void* hook_DH_get_text(void* self, IL2CPP::MethodInfo* mi) {
    void* ret = orig_DH_get_text ? orig_DH_get_text(self, mi) : nullptr;
    try {
        if (ret) {
            std::string s = ((BNM::Structures::Mono::String*)ret)->str();
            if (s.size() > 1500) DumpRespFile(s);
        }
    } catch (...) {}
    return ret;
}
static void InstallRespCapture() {
    try {
        Class dh("UnityEngine.Networking", "DownloadHandler");
        if (!dh.IsValid()) { BNM_FileCheckpoint("[resp] DownloadHandler NOT FOUND"); return; }
        MethodBase m = dh.GetMethod("get_text", 0);
        if (m.IsValid()) { BasicHook(m, hook_DH_get_text, orig_DH_get_text);
                           BNM_FileCheckpoint("[resp] DownloadHandler.get_text hooked"); }
        else BNM_FileCheckpoint("[resp] get_text NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("[resp] EXC"); }
}

// ---------------------------------------------------------------------
//  il2cpp-attach entry point (BNM). Kept minimal for 1.19.0: the BNM
//  Explorer HTTP server (so the game can be inspected/edited live) plus
//  the Debug.Log capture that feeds the Explorer's /logs view. The core
//  entitlement fix runs from JNI_OnLoad and does not depend on il2cpp.
// ---------------------------------------------------------------------
// ---------------------------------------------------------------------
//  1.31 account-validation bypass (release). The account coroutine
//  (RVA 0x287FD94) runs a client-side integrity check, "Gate B"
//  (RVA 0x256C0C8), over account fields we legitimately serve differently
//  on the private server, so it reports "Failed to validate account data".
//  Run the real check (side effects kept) and force it to pass, but only
//  while the account coroutine is active; other callers get the real result.
//  RVAs are libil2cpp file offsets; base = il2cpp_raise_exception - 0xFDD86C.
// ---------------------------------------------------------------------
static std::atomic<bool> g_inAcctCoroutine{false};
static void* (*orig_AcctCoroutine)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static void* hook_AcctCoroutine(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    g_inAcctCoroutine.store(true);
    void* r = orig_AcctCoroutine ? orig_AcctCoroutine(a0, a1, a2, a3, a4, a5, a6, a7) : nullptr;
    g_inAcctCoroutine.store(false);
    return r;
}
static uint64_t (*orig_AcctValidate)(void*, void*, void*, void*, void*, void*, void*, void*) = nullptr;
static uint64_t hook_AcctValidate(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    uint64_t r = orig_AcctValidate ? orig_AcctValidate(a0, a1, a2, a3, a4, a5, a6, a7) : 0;
    if (g_inAcctCoroutine.load() && (r & 1) == 0) return 1;
    return r;
}
static void InstallAccountValidateBypass() {
    void* exp = dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_raise_exception");
    if (!exp) { BNM_FileCheckpoint("AcctBypass: no il2cpp base"); return; }
    uint8_t* base = (uint8_t*)exp - 0xFDD86CULL;
    void* tr = nullptr;
    if (DobbyHook(base + 0x287FD94ULL, (dobby_dummy_func_t)hook_AcctCoroutine, (dobby_dummy_func_t*)&tr) == 0)
        orig_AcctCoroutine = (void*(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    tr = nullptr;
    if (DobbyHook(base + 0x256C0C8ULL, (dobby_dummy_func_t)hook_AcctValidate, (dobby_dummy_func_t*)&tr) == 0)
        orig_AcctValidate = (uint64_t(*)(void*, void*, void*, void*, void*, void*, void*, void*))tr;
    BNM_FileCheckpointF("AcctBypass: coroutine=%d validate=%d", orig_AcctCoroutine != nullptr, orig_AcctValidate != nullptr);
}

// =====================================================================
//  Remote client (build 131b2+): server announcements + remote disk audio.
//  Server side: yeeps-proxy vr-server services/remote.py, GET /client/feed.txt
//    A<TAB>id<TAB>seconds<TAB>text      (| = newline)
//    D<TAB>clipName<TAB>ogg|mp3|wav<TAB>url
//    S<TAB>slot<TAB>WxHxFrames<TAB>url     (picture screen, 131b3+)
//    F<TAB>accountID<TAB>1<TAB>-           (treat as friend, 131b6+)
//  Everything Unity-facing runs on the main thread, from a per-frame tick
//  (AccountOnlineManager.Update). Only stable UnityEngine APIs are used.
// =====================================================================
// feed URL comes from the kit settings: KitUrl("/client/feed.txt")

static void* (*p_gchandle_target)(void*) = nullptr;
static void (*p_gchandle_free)(void*) = nullptr;
static void* Pin(void* obj) {                 // keep a native-held object alive
    if (!obj || !p_gchandle_new) return nullptr;
    return p_gchandle_new(obj, false);
}
static void Unpin(void* h) { if (h && p_gchandle_free) p_gchandle_free(h); }

struct PendingReq { void* req = nullptr; void* handle = nullptr; void* op = nullptr; void* opHandle = nullptr; std::string clip; std::string ver; };
static PendingReq g_feedReq;
static std::vector<PendingReq> g_audioReqs;
static double g_nextFeedAt = 0;

static std::mutex g_diskMtx;
static std::map<std::string, void*> g_diskClipHandle;      // clip name -> pinned AudioClip handle
static std::map<std::string, std::string> g_diskVersion;   // clip name -> url (incl ?v=)
static std::map<void*, std::string> g_clipNameCache;       // original clip ptr -> name

struct Ann { std::string id, text; double seconds; };
static std::vector<Ann> g_annQueue;
static std::set<std::string> g_annSeen;
static void* g_annGO = nullptr;           // pinned GameObject handle
static void FollowHead(Il2CppObject* go);
static double g_annUntil = 0;

static std::string MonoStr(void* s) {
    if (!s) return "";
    try { return ((BNM::Structures::Mono::String*)s)->str(); } catch (...) { return ""; }
}
static bool Alive(void* obj) {
    if (!obj) return false;
    try {
        Method<bool> m(Class("UnityEngine", "Object").GetMethod("op_Implicit", 1));
        return m.IsValid() ? m((Il2CppObject*)obj) : true;
    } catch (...) { return false; }
}
// Pick an overload by exact parameter type names (e.g. Get(string) vs Get(Uri)).
static MethodBase FindOverload(Class c, const char* name, std::vector<std::string> types) {
    for (auto& m : c.GetMethods(false)) {
        auto* mi = m.GetInfo();
        if (!mi || !mi->name || strcmp(mi->name, name) != 0 || mi->parameters_count != types.size()) continue;
        bool ok = true;
        for (size_t i = 0; i < types.size() && ok; i++)
            ok = mi->parameters && mi->parameters[i] && typeName(mi->parameters[i]) == types[i];
        if (ok) return m;
    }
    return MethodBase();
}
static std::atomic<int> g_remoteLogN{0};
#define RLOG(...) do { if (g_remoteLogN.fetch_add(1) < 40) BNM_FileCheckpointF(__VA_ARGS__); } while (0)
extern "C" int stb_vorbis_decode_memory(const unsigned char* mem, int len, int* channels, int* sample_rate, short** output);
static void* (*p_object_new)(void*) = nullptr;
static void* (*p_array_new)(void*, uintptr_t) = nullptr;

// This build's engine code is stripped: no UnityWebRequest.Get, no
// request.isDone, no UnityWebRequestMultimedia. So: new UnityWebRequest(url,
// "GET") + DownloadHandlerBuffer, poll the AsyncOperation from SendWebRequest().
static bool StartGet(const std::string& url, PendingReq& out) {
    if (!p_object_new) return false;
    Class uwr("UnityEngine.Networking", "UnityWebRequest");
    Method<void> ctor(FindOverload(uwr, ".ctor", {"System.String", "System.String"}));
    Class dhb("UnityEngine.Networking", "DownloadHandlerBuffer");
    Method<void> dctor(FindOverload(dhb, ".ctor", {}));
    if (!ctor.IsValid() || !dctor.IsValid()) { RLOG("Remote: UnityWebRequest/DownloadHandlerBuffer ctor NOT FOUND"); return false; }
    auto* req = (Il2CppObject*)p_object_new(uwr.GetClass());
    auto* dh = (Il2CppObject*)p_object_new(dhb.GetClass());
    if (!req || !dh) return false;
    ctor[req](CreateMonoString(url.c_str()), CreateMonoString("GET"));
    dctor[dh]();
    Method<void>(uwr.GetMethod("set_downloadHandler", 1))[req](dh);
    Il2CppObject* op = Method<Il2CppObject*>(uwr.GetMethod("SendWebRequest", 0))[req]();
    if (!op) return false;
    out.req = req; out.handle = Pin(req); out.op = op; out.opHandle = Pin(op);
    return true;
}
static bool ReqDone(PendingReq& p) {
    Il2CppObject* op = p.opHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(p.opHandle) : nullptr;
    if (!op) return true;
    Method<bool> d(Class("UnityEngine", "AsyncOperation").GetMethod("get_isDone", 0));
    return !d.IsValid() || d[op]();
}
static bool ReqOk(void* r) {
    Method<Il2CppObject*> e(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("get_error", 0));
    return e.IsValid() && e[(Il2CppObject*)r]() == nullptr;
}
static std::string ReqText(void* r) {
    Il2CppObject* dh = Method<Il2CppObject*>(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("get_downloadHandler", 0))[(Il2CppObject*)r]();
    return dh ? MonoStr(Method<Il2CppObject*>(Class("UnityEngine.Networking", "DownloadHandler").GetMethod("get_text", 0))[dh]()) : "";
}
static void ReqDispose(PendingReq& p) {
    try {
        Method<void> d(Class("UnityEngine.Networking", "UnityWebRequest").GetMethod("Dispose", 0));
        if (p.req && d.IsValid()) d[(Il2CppObject*)p.req]();
    } catch (...) {}
    Unpin(p.handle); Unpin(p.opHandle);
    p = PendingReq{};
}

// ---- disk audio: base64 text -> OGG/WAV bytes -> mono float PCM (background)
struct DecodedDisk { std::string clip, ver; std::vector<float> pcm; int rate = 0; };
static std::mutex g_decMtx;
static std::vector<DecodedDisk> g_decoded;
static std::vector<uint8_t> B64Decode(const std::string& in) {
    static int T[256]; static bool init = false;
    if (!init) { for (int i = 0; i < 256; i++) T[i] = -1;
                 const char* a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
                 for (int i = 0; i < 64; i++) T[(uint8_t)a[i]] = i; init = true; }
    std::vector<uint8_t> out; out.reserve(in.size() * 3 / 4);
    int val = 0, bits = -8;
    for (uint8_t c : in) { if (T[c] < 0) continue; val = (val << 6) + T[c]; bits += 6;
                           if (bits >= 0) { out.push_back((uint8_t)((val >> bits) & 0xFF)); bits -= 8; } }
    return out;
}
static bool DecodeToMono(const std::vector<uint8_t>& b, std::vector<float>& pcm, int& rate) {
    if (b.size() > 4 && memcmp(b.data(), "OggS", 4) == 0) {
        int ch = 0, sr = 0; short* out = nullptr;
        int frames = stb_vorbis_decode_memory(b.data(), (int)b.size(), &ch, &sr, &out);
        if (frames <= 0 || !out || ch <= 0) { if (out) free(out); return false; }
        pcm.resize(frames);
        for (int i = 0; i < frames; i++) { int s = 0; for (int c = 0; c < ch; c++) s += out[i * ch + c]; pcm[i] = (s / (float)ch) / 32768.f; }
        free(out); rate = sr; return true;
    }
    if (b.size() > 44 && memcmp(b.data(), "RIFF", 4) == 0 && memcmp(b.data() + 8, "WAVE", 4) == 0) {
        size_t pos = 12; int ch = 0, sr = 0, bps = 0; const uint8_t* data = nullptr; uint32_t dlen = 0;
        while (pos + 8 <= b.size()) {
            uint32_t len; memcpy(&len, b.data() + pos + 4, 4);
            if (memcmp(b.data() + pos, "fmt ", 4) == 0) { ch = b[pos+10] | (b[pos+11] << 8); memcpy(&sr, b.data() + pos + 12, 4); bps = b[pos+22] | (b[pos+23] << 8); }
            else if (memcmp(b.data() + pos, "data", 4) == 0) { data = b.data() + pos + 8; dlen = (uint32_t)std::min<size_t>(len, b.size() - pos - 8); break; }
            pos += 8 + len + (len & 1);
        }
        if (!data || ch <= 0 || bps != 16) return false;
        size_t frames = dlen / (2 * ch); pcm.resize(frames);
        for (size_t i = 0; i < frames; i++) { int s = 0; for (int c = 0; c < ch; c++) { int16_t v; memcpy(&v, data + (i * ch + c) * 2, 2); s += v; } pcm[i] = (s / (float)ch) / 32768.f; }
        rate = sr; return true;
    }
    return false;
}
static void BuildClipOnMain(DecodedDisk& d) {
    if (!p_array_new || d.pcm.empty()) return;
    Class ac("UnityEngine", "AudioClip");
    Method<Il2CppObject*> create(FindOverload(ac, "Create", {"System.String", "System.Int32", "System.Int32", "System.Int32", "System.Boolean"}));
    Method<bool> setData(FindOverload(ac, "SetData", {"System.Single[]", "System.Int32"}));
    if (!create.IsValid() || !setData.IsValid()) { RLOG("Remote: AudioClip.Create/SetData NOT FOUND"); return; }
    Il2CppObject* clip = create(CreateMonoString(d.clip.c_str()), (int)d.pcm.size(), 1, d.rate, false);
    if (!clip) return;
    void* arr = p_array_new(Class("System", "Single").GetClass(), d.pcm.size());
    if (!arr) return;
    memcpy((uint8_t*)arr + 0x20, d.pcm.data(), d.pcm.size() * sizeof(float));   // Il2CppArray data at +0x20
    setData[clip]((Il2CppObject*)arr, 0);
    std::lock_guard<std::mutex> lk(g_diskMtx);
    Unpin(g_diskClipHandle[d.clip]);
    g_diskClipHandle[d.clip] = Pin(clip);
    g_diskVersion[d.clip] = d.ver;
    RLOG("Remote: disk %s ready (%d samples @ %d Hz)", d.clip.c_str(), (int)d.pcm.size(), d.rate);
}

// ---- picture screens (build 131b3+): S<TAB>slot<TAB>WxHxN<TAB>url
// The server converts the uploaded PNG/GIF into a small binary (base64):
//   "BSCR" u16 w, u16 h, u16 frames, u16 delayMs[frames], then zlib(RGB24
//   frames, bottom row first). Each frame becomes a Texture2D; the block's own
//   material (ResourceManager "blazescreen/mat_<slot>") shows the current frame,
//   so every placed copy of the block updates together.
struct DecodedScreen { std::string slot, ver; int w = 0, h = 0, n = 0; std::vector<int> delays; std::vector<uint8_t> pix; };
static std::mutex g_scrMtx;
static std::vector<DecodedScreen> g_scrDecoded;
static std::vector<PendingReq> g_scrReqs;
struct ScreenState {
    std::string ver;
    DecodedScreen cur;                 // current picture: all frames, CPU side
    void* tex = nullptr;               // pinned Texture2D, pixels rewritten in place
    void* buf = nullptr;               // pinned managed byte[] (one frame)
    int texW = 0, texH = 0;
    size_t frame = 0; double nextAt = 0, reapplyAt = 0;
    void* mat = nullptr;               // pinned Material handle
};
static std::map<std::string, ScreenState> g_screens;

static bool DecodeScreen(const std::vector<uint8_t>& b, DecodedScreen& d) {
    if (b.size() < 10 || memcmp(b.data(), "BSCR", 4) != 0) return false;
    auto u16 = [&](size_t o) { return (int)(b[o] | (b[o + 1] << 8)); };
    d.w = u16(4); d.h = u16(6); d.n = u16(8);
    if (d.w <= 0 || d.h <= 0 || d.n <= 0 || d.w > 1024 || d.h > 1024 || d.n > 120) return false;
    size_t off = 10 + 2 * (size_t)d.n;
    if (b.size() < off) return false;
    for (int i = 0; i < d.n; i++) d.delays.push_back(std::max(20, u16(10 + 2 * i)));
    uLongf want = (uLongf)d.w * d.h * 3 * d.n;
    d.pix.resize(want);
    uLongf got = want;
    if (uncompress(d.pix.data(), &got, b.data() + off, (uLong)(b.size() - off)) != Z_OK || got != want) return false;
    return true;
}
static Il2CppObject* ScreenMaterial(ScreenState& st, const std::string& slot) {
    Il2CppObject* m = st.mat && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.mat) : nullptr;
    if (m && Alive(m)) return m;
    Unpin(st.mat); st.mat = nullptr;
    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
    if (!load.IsValid()) return nullptr;
    m = load(CreateMonoString(("blazescreen/mat_" + slot).c_str()));
    if (m) st.mat = Pin(m);
    return m;
}
// The game renders map objects with its own COPIES of their materials (made
// when it batches/relights them), so setting the texture on our material alone
// only showed up when the game rebuilt those copies. Every few seconds, find
// every live material named blazeScreen_<slot>... and update all of them.
static std::map<std::string, std::vector<void*>> g_scrMatCopies;   // slot -> pinned materials
static std::map<void*, std::string> g_matNameSeen;                  // material ptr -> slot ("" = not ours)
static double g_nextMatScan = 0, g_nextSeenReset = 0;
static void ScanScreenMaterials(double now) {
    if (now < g_nextMatScan) return;
    g_nextMatScan = now + 3.0;
    if (now >= g_nextSeenReset) { g_matNameSeen.clear(); g_nextSeenReset = now + 60.0; }
    Class matC("UnityEngine", "Material");
    Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (!findAll.IsValid()) return;
    auto* arr = (uint8_t*)findAll((Il2CppObject*)matC.GetMonoType());
    if (!arr) return;
    uintptr_t n = *(uintptr_t*)(arr + 0x18);
    Method<Il2CppObject*> getName(Class("UnityEngine", "Object").GetMethod("get_name", 0));
    for (uintptr_t i = 0; i < n; i++) {
        void* m = ((void**)(arr + 0x20))[i];
        if (!m) continue;
        auto it = g_matNameSeen.find(m);
        if (it != g_matNameSeen.end()) continue;
        std::string slot;
        std::string name = MonoStr(getName[(Il2CppObject*)m]());
        if (name.rfind("blazeScreen_", 0) == 0) {
            slot = name.substr(12);
            size_t cut = slot.find_first_of(" (");
            if (cut != std::string::npos) slot = slot.substr(0, cut);
            auto& v = g_scrMatCopies[slot];
            v.push_back(Pin(m));
            g_screens[slot].reapplyAt = 0;     // push the current frame to it now
            static int found = 0;
            if (found++ < 12) BNM_FileCheckpointF("Remote: screen material copy '%s' -> %s (%d total)", name.c_str(), slot.c_str(), (int)v.size());
        }
        g_matNameSeen[m] = slot;
    }
    // drop destroyed copies
    for (auto& kv : g_scrMatCopies) {
        auto& v = kv.second;
        for (size_t i = 0; i < v.size();) {
            Il2CppObject* m = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(v[i]) : nullptr;
            if (!m || !Alive(m)) { Unpin(v[i]); v.erase(v.begin() + i); } else i++;
        }
    }
}
static void SetScreenTexture(ScreenState& st, const std::string& slot, Il2CppObject* tex) {
    if (!tex) return;
    Method<void> set(FindOverload(Class("UnityEngine", "Material"), "SetTexture", {"System.String", "UnityEngine.Texture"}));
    if (!set.IsValid()) return;
    Il2CppObject* m = ScreenMaterial(st, slot);
    if (m) set[m](CreateMonoString("_MainTex"), tex);
    for (void* h : g_scrMatCopies[slot]) {
        Il2CppObject* c = p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr;
        if (c && c != m) { try { set[c](CreateMonoString("_MainTex"), tex); } catch (...) {} }
    }
}
// One texture per screen; each GIF frame is written INTO it (LoadRawTextureData
// + Apply). The game renders our blocks through material copies / property
// blocks that keep a reference to the texture they saw, so swapping texture
// objects never showed up -- rewriting the same texture's pixels does.
static Il2CppObject* EnsureLiveTexture(ScreenState& st, int w, int h) {
    Il2CppObject* tex = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
    if (tex && Alive(tex) && st.texW == w && st.texH == h) return tex;
    if (tex) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(tex); } catch (...) {} }
    Unpin(st.tex); st.tex = nullptr; Unpin(st.buf); st.buf = nullptr;
    Class t2("UnityEngine", "Texture2D");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    if (!ctor.IsValid() || !p_object_new || !p_array_new) return nullptr;
    tex = (Il2CppObject*)p_object_new(t2.GetClass());
    if (!tex) return nullptr;
    ctor[tex](w, h, 3 /* RGB24 */, false);
    try {
        Class tx("UnityEngine", "Texture");
        Method<void>(tx.GetMethod("set_wrapMode", 1))[tex](1 /* Clamp */);
        Method<void>(tx.GetMethod("set_filterMode", 1))[tex](1 /* Bilinear */);
    } catch (...) {}
    void* arr = p_array_new(Class("System", "Byte").GetClass(), (size_t)w * h * 3);
    if (!arr) return nullptr;
    st.tex = Pin(tex); st.buf = Pin(arr); st.texW = w; st.texH = h;
    return tex;
}
static bool UploadFrame(ScreenState& st, size_t i) {
    const DecodedScreen& d = st.cur;
    Il2CppObject* tex = EnsureLiveTexture(st, d.w, d.h);
    void* arr = st.buf && p_gchandle_target ? p_gchandle_target(st.buf) : nullptr;
    if (!tex || !arr || (int)i >= d.n) return false;
    Class t2("UnityEngine", "Texture2D");
    Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
    Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
    if (!load.IsValid() || !apply.IsValid()) return false;
    size_t n = (size_t)d.w * d.h * 3;
    memcpy((uint8_t*)arr + 0x20, d.pix.data() + n * i, n);
    load[tex]((Il2CppObject*)arr);
    apply[tex](false, false);   // stays writable for the next frame
    return true;
}
static void ScreensTick(double now) {
    bool anyScreen = false;
    for (auto& kv : g_screens) if (!kv.second.ver.empty()) anyScreen = true;
    if (anyScreen) { try { ScanScreenMaterials(now); } catch (...) {} }
    for (size_t i = 0; i < g_scrReqs.size();) {
        auto& p = g_scrReqs[i];
        if (!ReqDone(p)) { i++; continue; }
        if (ReqOk(p.req)) {
            std::string b64 = ReqText(p.req);
            std::string slot = p.clip, ver = p.ver;
            std::thread([b64, slot, ver]() {
                DecodedScreen d; d.slot = slot; d.ver = ver;
                if (DecodeScreen(B64Decode(b64), d)) { std::lock_guard<std::mutex> lk(g_scrMtx); g_scrDecoded.push_back(std::move(d)); }
                else BNM_FileCheckpointF("Remote: screen %s decode failed", slot.c_str());
            }).detach();
        } else RLOG("Remote: screen %s download failed", p.clip.c_str());
        ReqDispose(p);
        g_scrReqs.erase(g_scrReqs.begin() + i);
    }
    {
        std::vector<DecodedScreen> ready;
        { std::lock_guard<std::mutex> lk(g_scrMtx); ready.swap(g_scrDecoded); }
        for (auto& d : ready) {
            auto& st = g_screens[d.slot];
            st.ver = d.ver; st.cur = std::move(d);
            st.frame = 0; st.nextAt = 0; st.reapplyAt = 0;
            bool ok = false;
            try { ok = UploadFrame(st, 0); } catch (...) {}
            BNM_FileCheckpointF("Remote: screen %s ready (%d frames, %dx%d) upload=%d", st.cur.slot.c_str(), st.cur.n, st.cur.w, st.cur.h, ok);
        }
    }
    for (auto& kv : g_screens) {
        const std::string& slot = kv.first;
        auto& st = kv.second;
        if (st.ver.empty() || st.cur.n <= 0) continue;
        if (st.cur.n > 1 && now >= st.nextAt) {
            st.frame = (st.frame + 1) % st.cur.n;
            st.nextAt = now + st.cur.delays[st.frame % st.cur.delays.size()] / 1000.0;
            try { UploadFrame(st, st.frame); } catch (...) {}
        }
        if (now >= st.reapplyAt) {
            st.reapplyAt = now + 2.0;
            Il2CppObject* t = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
            try { SetScreenTexture(st, slot, t); } catch (...) {}
        }
    }
}
static void ClearScreen(const std::string& slot) {
    auto it = g_screens.find(slot);
    if (it == g_screens.end()) return;
    auto& st = it->second;
    Il2CppObject* t = st.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(st.tex) : nullptr;
    if (t) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(t); } catch (...) {} }
    Unpin(st.tex); st.tex = nullptr; Unpin(st.buf); st.buf = nullptr;
    st.ver.clear(); st.cur = DecodedScreen{};
}

// The game re-assigns every map object's materials (paint colour etc.) through
// MapObject.SetSharedMaterials, which replaced the screen's picture material
// with the shared decoration atlas. For our screen blocks, keep slot 1 (the
// picture surface) on the screen's own material; slot 0 (the frame) still
// takes the game's colour material, so painting keeps working.
// Only the BLAZE Screen gets the picture material. Swapping the material on the
// 3x3 Screen (tag-0 decoration) crashed every client loading a big public room
// (official_1 / wild_0) - left alone it just renders plain.
static const std::map<std::string, std::string> kScreenKeys = {
    {"3x2x1_blazeScreenRole", "blaze"}};
// Picture blocks: slot 1 = a fixed picture material shipped in the game data
// (tools/data/add_picture_block.py), registered at the Resources path
// "blazepic/mat_<block key, lowercase>". Every block key is looked up once.
static std::map<std::string, void*> g_pictureMats;     // path -> pinned material
static std::map<std::string, std::string> g_pictureKeyPath;   // block key -> path ("" = not a picture block)
static Il2CppObject* PictureMaterial(const std::string& path) {
    auto f = g_pictureMats.find(path);
    if (f != g_pictureMats.end() && p_gchandle_target) {
        auto* m = (Il2CppObject*)p_gchandle_target(f->second);
        if (m && Alive(m)) return m;
        Unpin(f->second); g_pictureMats.erase(f);
    }
    Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
    if (!load.IsValid()) return nullptr;
    Il2CppObject* m = load(CreateMonoString(path.c_str()));
    if (m) g_pictureMats[path] = Pin(m);
    return m;
}
static std::string PicturePathFor(const std::string& key) {
    auto it = g_pictureKeyPath.find(key);
    if (it != g_pictureKeyPath.end()) return it->second;
    std::string lower = key;
    for (auto& ch : lower) ch = (char)tolower((unsigned char)ch);
    std::string path = "blazepic/mat_" + lower;
    std::string found = PictureMaterial(path) ? path : std::string();
    g_pictureKeyPath[key] = found;
    return found;
}
static void (*orig_MO_SetSharedMaterials)(void*, void*, void*) = nullptr;
static void hook_MO_SetSharedMaterials(void* self, void* arr, void* mi) {
    void* pass = arr;
    try {
        if (self && arr && p_array_new) {
            Class mo("", "MapObject", Image("Assembly-CSharp.dll"));
            std::string key = MonoStr(Method<Il2CppObject*>(mo.GetMethod("get_key", 0))[(Il2CppObject*)self]());
            if (key.empty()) {
                Il2CppObject* go = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[(Il2CppObject*)self]();
                if (go) key = MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[go]());
                size_t c = key.find("(Clone)"); if (c != std::string::npos) key = key.substr(0, c);
            }
            std::string picPath = key.empty() ? std::string() : PicturePathFor(key);
            if (!picPath.empty()) {
                uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);     // Il2CppArray max_length
                Il2CppObject* m = PictureMaterial(picPath);
                if (n >= 2 && m) {
                    void* copy = p_array_new(Class("UnityEngine", "Material").GetClass(), n);
                    if (copy) {
                        memcpy((uint8_t*)copy + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
                        ((void**)((uint8_t*)copy + 0x20))[1] = m;
                        pass = copy;
                    }
                }
                static int plogged = 0;
                if (plogged++ < 6) BNM_FileCheckpointF("Remote: picture material for %s (n=%d, mat=%d)", key.c_str(), (int)n, m != nullptr);
            }
            auto it = kScreenKeys.find(key);
            if (it != kScreenKeys.end()) {
                uintptr_t n = *(uintptr_t*)((uint8_t*)arr + 0x18);     // Il2CppArray max_length
                Il2CppObject* m = ScreenMaterial(g_screens[it->second], it->second);
                if (n >= 2 && m) {
                    void* copy = p_array_new(Class("UnityEngine", "Material").GetClass(), n);
                    if (copy) {
                        memcpy((uint8_t*)copy + 0x20, (uint8_t*)arr + 0x20, n * sizeof(void*));
                        ((void**)((uint8_t*)copy + 0x20))[1] = m;
                        pass = copy;
                    }
                }
                static int logged = 0;
                if (logged++ < 6) BNM_FileCheckpointF("Remote: screen materials for %s (n=%d, mat=%d)", key.c_str(), (int)n, m != nullptr);
            }
        }
    } catch (...) {}
    if (orig_MO_SetSharedMaterials) orig_MO_SetSharedMaterials(self, pass, mi);
}

// ---- "everyone is a friend" (131b6+): F<TAB>accountID lines from the feed.
// Meta friends always fail (400) on this server, so friends lists were empty
// and friends-only private worlds couldn't be joined. The server publishes who
// is online; FriendManager.IsFriend() also accepts them, and they're added to
// the game's friend list so they show in the friends UI.
static std::set<std::string> g_feedFriends;
static bool g_feedFriendsDirty = false;
static std::mutex g_feedFriendsMtx;
static bool (*orig_FM_IsFriend)(void*, void*) = nullptr;
static bool hook_FM_IsFriend(void* accountID, void* mi) {
    bool r = orig_FM_IsFriend ? orig_FM_IsFriend(accountID, mi) : false;
    if (r || !accountID) return r;
    std::string id = MonoStr(accountID);
    std::lock_guard<std::mutex> lk(g_feedFriendsMtx);
    return g_feedFriends.count(id) > 0;
}
static double g_nextFriendSync = 0;          // friend list refresh: every 3 minutes
static void SyncFeedFriendsIntoGame() {
    double now = nowSeconds();
    std::set<std::string> want;
    { std::lock_guard<std::mutex> lk(g_feedFriendsMtx);
      if (!g_feedFriendsDirty || now < g_nextFriendSync) return;
      g_feedFriendsDirty = false; want = g_feedFriends; }
    g_nextFriendSync = now + 180.0;
    Class fm("", "FriendManager", Image("Assembly-CSharp.dll"));
    Method<Il2CppObject*> getList(fm.GetMethod("get_friendAccountIDs", 0));
    if (!getList.IsValid()) return;
    auto* list = (uint8_t*)getList();
    if (!list) return;
    std::string self;
    try { self = MonoStr(Method<Il2CppObject*>(Class("", "AccountManager", Image("Assembly-CSharp.dll")).GetMethod("get_accountID", 0))()); } catch (...) {}
    std::set<std::string> have;
    auto* items = *(uint8_t**)(list + 0x10);
    int size = *(int*)(list + 0x18);
    Il2CppClass* strK = Class("System", "String").GetClass();
    if (items && size > 0 && size < 100000)
        for (int i = 0; i < size; i++) {
            auto* e = ((Il2CppObject**)(items + 0x20))[i];
            if (e && e->klass == strK) have.insert(MonoStr(e));
        }
    Method<void> add(Class(((Il2CppObject*)list)->klass).GetMethod("Add", 1));
    if (!add.IsValid()) return;
    int added = 0;
    for (auto& id : want) {
        if (id == self || have.count(id)) continue;
        add[(Il2CppObject*)list](CreateMonoString(id.c_str()));
        added++;
    }
    static int logged = 0;
    if (added && logged++ < 5) BNM_FileCheckpointF("Friends: added %d online players to the friend list", added);
    char fb[200];
    snprintf(fb, sizeof(fb), "[FriendsDiag] friend list: %d in game list, %d online from server, %d added now",
             (int)have.size(), (int)want.size(), added);
    GameLog(fb);
}

// ---- floating video (131b8+): parsed from the feed's V line, applied by
// VideoTick on the main thread. See the video player below RemoteTick.
struct VideoWanted { std::string id, segBase, audioUrl; int w = 0, h = 0, fps = 10, nSeg = 0, segFrames = 60, frames = 0; bool loop = true; };
static VideoWanted g_vidWanted;
static bool g_vidWantedSet = false;
static std::mutex g_vidReqMtx;

static void ParseFeed(const std::string& body) {
    std::set<std::string> seenScreens;
    std::set<std::string> feedFriends;
    bool seenVideo = false; VideoWanted wantVideo;
    size_t pos = 0;
    while (pos < body.size()) {
        size_t nl = body.find('\n', pos);
        std::string line = body.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? body.size() : nl + 1;
        std::vector<std::string> f;
        size_t a = 0, b;
        while ((b = line.find('\t', a)) != std::string::npos && f.size() < 3) { f.push_back(line.substr(a, b - a)); a = b + 1; }
        f.push_back(line.substr(a));
        if (f.size() < 4) continue;
        if (f[0] == "A" && !g_annSeen.count(f[1])) {
            g_annSeen.insert(f[1]);
            std::string t = f[3];
            for (auto& c : t) if (c == '|') c = '\n';
            double secs = 10; try { secs = std::stod(f[2]); } catch (...) {}
            g_annQueue.push_back({f[1], t, secs});
        } else if (f[0] == "F") {
            feedFriends.insert(f[1]);
        } else if (f[0] == "S") {
            seenScreens.insert(f[1]);
            if (g_screens[f[1]].ver == f[3]) continue;
            bool inflight = false;
            for (auto& p : g_scrReqs) if (p.clip == f[1] && p.ver == f[3]) inflight = true;
            if (inflight) continue;
            PendingReq pr; pr.clip = f[1]; pr.ver = f[3];
            std::string url = f[3];
            size_t q = url.find('?');
            url = (q == std::string::npos ? url : url.substr(0, q)) + ".b64" + (q == std::string::npos ? "" : url.substr(q));
            if (StartGet(url, pr)) { g_scrReqs.push_back(pr); RLOG("Remote: downloading screen %s", f[1].c_str()); }
        } else if (f[0] == "D") {
            std::lock_guard<std::mutex> lk(g_diskMtx);
            if (g_diskVersion[f[1]] == f[3]) continue;          // already have this version
            bool inflight = false;
            for (auto& p : g_audioReqs) if (p.clip == f[1] && p.ver == f[3]) inflight = true;
            if (inflight) continue;
            PendingReq pr; pr.clip = f[1]; pr.ver = f[3];
            std::string url = f[3];
            size_t q = url.find('?');
            url = (q == std::string::npos ? url : url.substr(0, q)) + ".b64" + (q == std::string::npos ? "" : url.substr(q));
            if (StartGet(url, pr)) { g_audioReqs.push_back(pr); RLOG("Remote: downloading disk %s", f[1].c_str()); }
        } else if (f[0] == "V") {
            // V has more fields than the capped split kept -> re-split the whole line.
            std::vector<std::string> g; size_t ga = 0, gb;
            while ((gb = line.find('\t', ga)) != std::string::npos) { g.push_back(line.substr(ga, gb - ga)); ga = gb + 1; }
            g.push_back(line.substr(ga));
            if (g.size() >= 7) {
                VideoWanted vw; vw.id = g[1];
                int w = 0, h = 0, fps = 10; sscanf(g[2].c_str(), "%dx%dx%d", &w, &h, &fps);
                vw.w = w; vw.h = h; vw.fps = fps > 0 ? fps : 10;
                vw.nSeg = atoi(g[3].c_str()); vw.loop = (g[4] != "0");
                vw.audioUrl = g[5]; vw.segBase = g[6];
                if (vw.w > 0 && vw.h > 0 && vw.nSeg > 0) { seenVideo = true; wantVideo = vw; }
            }
        }
    }
    for (auto& kv : g_screens)
        if (!seenScreens.count(kv.first) && !kv.second.ver.empty()) ClearScreen(kv.first);
    { std::lock_guard<std::mutex> lk(g_feedFriendsMtx); g_feedFriends.swap(feedFriends); g_feedFriendsDirty = true; }
    // 131b9: video is driven ONLY by the 3 s /client/video.txt poll (VideoTick).
    // The feed's V line is for 131b8 clients; applying both could flap.
    (void)seenVideo; (void)wantVideo;
}

// This build has no TextMesh / TextMeshPro (stripped), so announcements are a
// small world-space Canvas with a UI Text (the game's own text type). The font
// is taken from the game's version label (cached in hook_AVDU_Start).
static void* g_lilitaHandle = nullptr;
static Il2CppObject* AnnouncementFont() {
    // Lilita One (added to the game data as ResourceManager "blaze/lilitaone").
    Il2CppObject* f = g_lilitaHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_lilitaHandle) : nullptr;
    if (f && Alive(f)) return f;
    try {
        Method<Il2CppObject*> load(FindOverload(Class("UnityEngine", "Resources"), "Load", {"System.String"}));
        f = load.IsValid() ? load(CreateMonoString("blaze/lilitaone")) : nullptr;
        if (f) { g_lilitaHandle = Pin(f); return f; }
    } catch (...) {}
    f = g_uiFontHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_uiFontHandle) : nullptr;
    if (f) return f;
    // Use any font the game already has loaded. (GetBuiltinResource for
    // LegacyRuntime.ttf / Arial.ttf is stripped in this build and logged an error.)
    Il2CppObject* fontType = sysTypeOf("UnityEngine.Font, UnityEngine.TextRenderingModule");
    Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (fontType && findAll.IsValid()) {
        auto* arr = (uint8_t*)findAll(fontType);
        if (arr && *(uintptr_t*)(arr + 0x18) > 0) {
            f = ((Il2CppObject**)(arr + 0x20))[0];
            if (f && p_gchandle_new) g_uiFontHandle = Pin(f);
        }
    }
    return f;
}
static void ShowAnnouncement(const Ann& a) {
    try {
        Class goc("UnityEngine", "GameObject");
        Method<Il2CppObject*> addComp(goc.GetMethod("AddComponent", 1));
        Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
        Il2CppObject* canvasType = sysTypeOf("UnityEngine.Canvas, UnityEngine.UIModule");
        Il2CppObject* textType = sysTypeOf("UnityEngine.UI.Text, UnityEngine.UI");
        Il2CppObject* font = AnnouncementFont();
        if (!addComp.IsValid() || !canvasType || !textType) { RLOG("Remote: announcement UI types NOT FOUND"); return; }
        if (!font) { RLOG("Remote: announcement font NOT FOUND"); return; }
        Il2CppObject* go = goc.CreateNewObjectParameters(CreateMonoString("BlazeAnnouncement"));
        if (!go) return;
        Il2CppObject* canvas = addComp[go](canvasType);   // also swaps Transform -> RectTransform
        if (!canvas) { RLOG("Remote: AddComponent(Canvas) failed"); return; }
        Class cv("UnityEngine", "Canvas");
        Method<void>(cv.GetMethod("set_renderMode", 1))[canvas](2);        // RenderMode.WorldSpace
        Method<void>(cv.GetMethod("set_sortingOrder", 1))[canvas](1000);
        Class rt("UnityEngine", "RectTransform");
        Class tr("UnityEngine", "Transform");
        Il2CppObject* rootT = getTr[go]();
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[rootT](Vector2(900.f, 300.f));
        Method<void>(tr.GetMethod("set_localScale", 1))[rootT](Vector3(0.0018f, 0.0018f, 0.0018f));
        Il2CppObject* child = goc.CreateNewObjectParameters(CreateMonoString("Text"));
        Il2CppObject* text = child ? addComp[child](textType) : nullptr;
        if (!text) { RLOG("Remote: AddComponent(Text) failed"); return; }
        Il2CppObject* childT = getTr[child]();
        Method<void>(FindOverload(tr, "SetParent", {"UnityEngine.Transform", "System.Boolean"}))[childT](rootT, false);
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[childT](Vector2(900.f, 300.f));
        Class tx("UnityEngine.UI", "Text");
        Method<void>(tx.GetMethod("set_font", 1))[text](font);
        Method<void>(tx.GetMethod("set_text", 1))[text](CreateMonoString(a.text.c_str()));
        Method<void>(tx.GetMethod("set_fontSize", 1))[text](52);
        Method<void>(tx.GetMethod("set_alignment", 1))[text](4);            // TextAnchor.MiddleCenter
        Method<void>(tx.GetMethod("set_horizontalOverflow", 1))[text](0);   // Wrap
        Method<void>(tx.GetMethod("set_verticalOverflow", 1))[text](1);     // Overflow
        Class gr("UnityEngine.UI", "Graphic");
        Method<void>(gr.GetMethod("set_color", 1))[text](Color(1.f, 1.f, 1.f, 1.f));   // white
        Method<void>(gr.GetMethod("set_raycastTarget", 1))[text](false);
        // dark outline so white text stays readable on bright backgrounds
        try {
            Il2CppObject* outlineType = sysTypeOf("UnityEngine.UI.Outline, UnityEngine.UI");
            Il2CppObject* ol = outlineType ? addComp[child](outlineType) : nullptr;
            if (ol) {
                Class sh("UnityEngine.UI", "Shadow");
                Method<void>(sh.GetMethod("set_effectColor", 1))[ol](Color(0.f, 0.f, 0.f, 0.85f));
                Method<void>(sh.GetMethod("set_effectDistance", 1))[ol](Vector2(2.5f, -2.5f));
            }
        } catch (...) {}
        g_annGO = Pin(go);
        g_annUntil = nowSeconds() + a.seconds;
        FollowHead(go);
        RLOG("Remote: announcement shown (%s)", a.id.c_str());
    } catch (...) { RLOG("Remote: announcement EXCEPTION"); }
}
// The head camera's Transform. Camera.main is null in this rig (the VR camera
// object is named "MainCamera" but not tagged), so find it by name; cached.
static void* g_headTHandle = nullptr;
static Il2CppObject* HeadTransform() {
    Il2CppObject* t = g_headTHandle && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_headTHandle) : nullptr;
    if (t && Alive(t)) return t;
    Unpin(g_headTHandle); g_headTHandle = nullptr; t = nullptr;
    Class goc("UnityEngine", "GameObject");
    Method<Il2CppObject*> find(FindOverload(goc, "Find", {"System.String"}));
    Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
    for (const char* n : {"MainCamera", "CenterEyeAnchor"}) {
        Il2CppObject* go = find.IsValid() ? find(CreateMonoString(n)) : nullptr;
        if (go) { t = getTr[go](); break; }
    }
    if (!t) {
        Il2CppObject* cam = Method<Il2CppObject*>(Class("UnityEngine", "Camera").GetMethod("get_main", 0))();
        if (cam) t = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_transform", 0))[cam]();
    }
    if (t) g_headTHandle = Pin(t);
    return t;
}
static void FollowHead(Il2CppObject* go) {
    try {
        Il2CppObject* ct = HeadTransform();
        Il2CppObject* gt = Method<Il2CppObject*>(Class("UnityEngine", "GameObject").GetMethod("get_transform", 0))[go]();
        if (!ct || !gt) return;
        Class tr("UnityEngine", "Transform");
        Vector3 p = Method<Vector3>(tr.GetMethod("get_position", 0))[ct]();
        Vector3 fw = Method<Vector3>(tr.GetMethod("get_forward", 0))[ct]();
        Quaternion q = Method<Quaternion>(tr.GetMethod("get_rotation", 0))[ct]();
        Vector3 np(p.x + fw.x * 1.4f, p.y + fw.y * 1.4f + 0.15f, p.z + fw.z * 1.4f);
        Method<void>(tr.GetMethod("set_position", 1))[gt](np);
        Method<void>(tr.GetMethod("set_rotation", 1))[gt](q);
    } catch (...) {}
}

// =====================================================================
//  Floating video (131b8+). A full-length video + audio on a world-space
//  panel in front of every player, looping until the server stops sending
//  the V line. Frames reuse the picture-screen "BSCR" segment format (so
//  DecodeScreen decodes them) and audio reuses the disk OGG->PCM path. The
//  displayed frame is keyed to the AudioSource's playback time, so picture
//  and sound stay in sync and loop together. Segments are kept COMPRESSED
//  and the current one decoded on demand, so memory stays ~one segment.
struct VideoPlayer {
    std::string id, segBase, audioUrl;
    int w = 0, h = 0, fps = 10, nSeg = 0, segFrames = 60; bool loop = true, audioWanted = false;
    // download (sequential; playback starts after segment 0, the rest stream in)
    int segNext = 0; PendingReq segReq; bool segReqActive = false;
    PendingReq audioReq; bool audioReqActive = false;
    std::vector<std::vector<uint8_t>> segBlobs;   // compressed BSCR per segment
    std::vector<int> segStart;                    // prefix frame counts (nSeg+1)
    int totalFrames = 0; bool allSegs = false;
    // audio decode (worker thread)
    bool audioDecoding = false, audioDecoded = false;
    std::vector<float> pcm; int pcmRate = 0;
    double loadStart = 0;
    // built objects (all pinned)
    bool built = false;
    void* go = nullptr; void* rawImg = nullptr; void* tex = nullptr; void* buf = nullptr;
    void* audioSrc = nullptr; void* clip = nullptr;
    // display
    DecodedScreen cache; int cacheSeg = -1; int curFrame = -1; double startAt = 0;
};
static VideoPlayer g_vp;
static std::mutex g_vpAudMtx;
static bool g_vpAudDone = false, g_vpAudOk = false;
static std::vector<float> g_vpAudPcm; static int g_vpAudRate = 0;

static void VideoFreeObjects() {
    try {
        Il2CppObject* src = g_vp.audioSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.audioSrc) : nullptr;
        if (src && Alive(src)) { try { Method<void>(Class("UnityEngine", "AudioSource").GetMethod("Stop", 0))[src](); } catch (...) {} }
        Il2CppObject* go = g_vp.go && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.go) : nullptr;
        if (go && Alive(go)) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(go); } catch (...) {} }
        Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
        if (tex && Alive(tex)) { try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(tex); } catch (...) {} }
    } catch (...) {}
    Unpin(g_vp.go); Unpin(g_vp.rawImg); Unpin(g_vp.tex); Unpin(g_vp.buf); Unpin(g_vp.audioSrc); Unpin(g_vp.clip);
    g_vp.go = g_vp.rawImg = g_vp.tex = g_vp.buf = g_vp.audioSrc = g_vp.clip = nullptr;
}
static void StopVideo() {
    bool had = !g_vp.id.empty();
    if (g_vp.segReqActive) { ReqDispose(g_vp.segReq); }
    if (g_vp.audioReqActive) { ReqDispose(g_vp.audioReq); }
    VideoFreeObjects();
    g_vp = VideoPlayer{};
    { std::lock_guard<std::mutex> lk(g_vpAudMtx); g_vpAudDone = false; g_vpAudOk = false; g_vpAudPcm.clear(); }
    if (had) BNM_FileCheckpoint("Video: stopped");
}
static void StartVideoLoad(const VideoWanted& w) {
    StopVideo();
    g_vp.id = w.id; g_vp.w = w.w; g_vp.h = w.h; g_vp.fps = w.fps > 0 ? w.fps : 10;
    g_vp.nSeg = w.nSeg; g_vp.loop = w.loop; g_vp.segBase = w.segBase; g_vp.audioUrl = w.audioUrl;
    g_vp.segFrames = w.segFrames > 0 ? w.segFrames : 60;
    g_vp.totalFrames = w.frames > 0 ? w.frames : w.nSeg * g_vp.segFrames;   // known up front -> progressive
    g_vp.audioWanted = (!w.audioUrl.empty() && w.audioUrl != "-");
    g_vp.segStart.clear(); g_vp.segStart.push_back(0);
    g_vp.loadStart = nowSeconds();
    BNM_FileCheckpointF("Video: loading %s (%dx%d@%d, %d segs, loop=%d, audio=%d)",
                        w.id.c_str(), w.w, w.h, g_vp.fps, w.nSeg, (int)w.loop, (int)g_vp.audioWanted);
}
static bool VideoBuildTexture() {
    Class t2("UnityEngine", "Texture2D");
    Method<void> ctor(FindOverload(t2, ".ctor", {"System.Int32", "System.Int32", "UnityEngine.TextureFormat", "System.Boolean"}));
    if (!ctor.IsValid() || !p_object_new || !p_array_new) return false;
    Il2CppObject* tex = (Il2CppObject*)p_object_new(t2.GetClass());
    if (!tex) return false;
    ctor[tex](g_vp.w, g_vp.h, 3 /* RGB24 */, false);
    try {
        Class tx("UnityEngine", "Texture");
        Method<void>(tx.GetMethod("set_wrapMode", 1))[tex](1);
        Method<void>(tx.GetMethod("set_filterMode", 1))[tex](1);
    } catch (...) {}
    void* arr = p_array_new(Class("System", "Byte").GetClass(), (size_t)g_vp.w * g_vp.h * 3);
    if (!arr) return false;
    g_vp.tex = Pin(tex); g_vp.buf = Pin(arr);
    return true;
}
static void VideoUploadFrame(int local) {
    Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
    void* arr = g_vp.buf && p_gchandle_target ? p_gchandle_target(g_vp.buf) : nullptr;
    if (!tex || !arr || local < 0 || local >= g_vp.cache.n) return;
    Class t2("UnityEngine", "Texture2D");
    Method<void> load(FindOverload(t2, "LoadRawTextureData", {"System.Byte[]"}));
    Method<void> apply(FindOverload(t2, "Apply", {"System.Boolean", "System.Boolean"}));
    if (!load.IsValid() || !apply.IsValid()) return;
    size_t n = (size_t)g_vp.w * g_vp.h * 3;
    memcpy((uint8_t*)arr + 0x20, g_vp.cache.pix.data() + n * local, n);
    load[tex]((Il2CppObject*)arr);
    apply[tex](false, false);
}
static void VideoBuild(double now) {
    try {
        if (!VideoBuildTexture()) { BNM_FileCheckpoint("Video: texture build FAILED"); StopVideo(); return; }
        Il2CppObject* clip = nullptr;
        if (g_vp.audioWanted && !g_vp.pcm.empty() && p_array_new) {
            Class ac("UnityEngine", "AudioClip");
            Method<Il2CppObject*> create(FindOverload(ac, "Create", {"System.String", "System.Int32", "System.Int32", "System.Int32", "System.Boolean"}));
            Method<bool> setData(FindOverload(ac, "SetData", {"System.Single[]", "System.Int32"}));
            if (create.IsValid() && setData.IsValid()) {
                clip = create(CreateMonoString("BlazeVideoAudio"), (int)g_vp.pcm.size(), 1, g_vp.pcmRate > 0 ? g_vp.pcmRate : 44100, false);
                if (clip) {
                    void* a = p_array_new(Class("System", "Single").GetClass(), g_vp.pcm.size());
                    if (a) { memcpy((uint8_t*)a + 0x20, g_vp.pcm.data(), g_vp.pcm.size() * sizeof(float)); setData[clip]((Il2CppObject*)a, 0); }
                    g_vp.clip = Pin(clip);
                }
            }
        }
        Class goc("UnityEngine", "GameObject");
        Method<Il2CppObject*> addComp(goc.GetMethod("AddComponent", 1));
        Method<Il2CppObject*> getTr(goc.GetMethod("get_transform", 0));
        Il2CppObject* canvasType = sysTypeOf("UnityEngine.Canvas, UnityEngine.UIModule");
        Il2CppObject* rawType = sysTypeOf("UnityEngine.UI.RawImage, UnityEngine.UI");
        if (!addComp.IsValid() || !canvasType || !rawType) { BNM_FileCheckpoint("Video: UI types NOT FOUND"); StopVideo(); return; }
        Il2CppObject* go = goc.CreateNewObjectParameters(CreateMonoString("BlazeVideo"));
        if (!go) { StopVideo(); return; }
        Il2CppObject* canvas = addComp[go](canvasType);
        if (!canvas) { StopVideo(); return; }
        Class cv("UnityEngine", "Canvas");
        Method<void>(cv.GetMethod("set_renderMode", 1))[canvas](2);        // WorldSpace
        Method<void>(cv.GetMethod("set_sortingOrder", 1))[canvas](999);
        Class rt("UnityEngine", "RectTransform");
        Class tr("UnityEngine", "Transform");
        Il2CppObject* rootT = getTr[go]();
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[rootT](Vector2(800.f, 450.f));
        Method<void>(tr.GetMethod("set_localScale", 1))[rootT](Vector3(0.0024f, 0.0024f, 0.0024f));
        Il2CppObject* child = goc.CreateNewObjectParameters(CreateMonoString("RawImage"));
        Il2CppObject* raw = child ? addComp[child](rawType) : nullptr;
        if (!raw) { StopVideo(); return; }
        Il2CppObject* childT = getTr[child]();
        Method<void>(FindOverload(tr, "SetParent", {"UnityEngine.Transform", "System.Boolean"}))[childT](rootT, false);
        Method<void>(rt.GetMethod("set_sizeDelta", 1))[childT](Vector2(800.f, 450.f));
        Il2CppObject* tex = g_vp.tex && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.tex) : nullptr;
        try { Method<void>(Class("UnityEngine.UI", "RawImage").GetMethod("set_texture", 1))[raw](tex); } catch (...) {}
        try { Method<void>(Class("UnityEngine.UI", "Graphic").GetMethod("set_raycastTarget", 1))[raw](false); } catch (...) {}
        g_vp.go = Pin(go); g_vp.rawImg = Pin(raw);
        if (clip) {
            Il2CppObject* asType = sysTypeOf("UnityEngine.AudioSource, UnityEngine.AudioModule");
            Il2CppObject* src = asType ? addComp[go](asType) : nullptr;
            if (src) {
                Class as("UnityEngine", "AudioSource");
                try { Method<void>(as.GetMethod("set_clip", 1))[src](clip); } catch (...) {}
                try { Method<void>(as.GetMethod("set_loop", 1))[src](g_vp.loop); } catch (...) {}
                try { Method<void>(as.GetMethod("set_spatialBlend", 1))[src](0.0f); } catch (...) {}   // 2D: audible everywhere
                try { Method<void>(as.GetMethod("set_volume", 1))[src](1.0f); } catch (...) {}
                g_vp.audioSrc = Pin(src);
                try { Method<void>(as.GetMethod("Play", 0))[src](); } catch (...) {}
            }
        }
        g_vp.built = true; g_vp.startAt = now; g_vp.curFrame = -1;
        if (!g_vp.segBlobs.empty() && DecodeScreen(g_vp.segBlobs[0], g_vp.cache)) { g_vp.cacheSeg = 0; VideoUploadFrame(0); }
        FollowHead(go);
        BNM_FileCheckpointF("Video: PLAYING %s (%d frames @%dfps, %dx%d, audio=%d)",
                            g_vp.id.c_str(), g_vp.totalFrames, g_vp.fps, g_vp.w, g_vp.h, (int)(clip != nullptr));
    } catch (...) { BNM_FileCheckpoint("Video: build EXCEPTION"); StopVideo(); }
}
static void VideoPump(double now) {
    // audio: one request, decoded on a worker thread
    if (g_vp.audioWanted && !g_vp.audioReqActive && !g_vp.audioDecoded && !g_vp.audioDecoding && g_vp.pcm.empty()) {
        if (StartGet(g_vp.audioUrl, g_vp.audioReq)) g_vp.audioReqActive = true;
        else g_vp.audioWanted = false;
    }
    if (g_vp.audioReqActive && ReqDone(g_vp.audioReq)) {
        if (ReqOk(g_vp.audioReq.req)) {
            std::string b64 = ReqText(g_vp.audioReq.req);
            g_vp.audioDecoding = true;
            { std::lock_guard<std::mutex> lk(g_vpAudMtx); g_vpAudDone = false; }
            std::thread([b64]() {
                std::vector<float> pcm; int rate = 0; bool ok = DecodeToMono(B64Decode(b64), pcm, rate);
                std::lock_guard<std::mutex> lk(g_vpAudMtx);
                g_vpAudPcm.swap(pcm); g_vpAudRate = rate; g_vpAudOk = ok; g_vpAudDone = true;
            }).detach();
        } else { g_vp.audioWanted = false; }   // audio failed -> play silent
        ReqDispose(g_vp.audioReq); g_vp.audioReqActive = false;
    }
    if (g_vp.audioDecoding) {
        std::lock_guard<std::mutex> lk(g_vpAudMtx);
        if (g_vpAudDone) {
            if (g_vpAudOk) { g_vp.pcm.swap(g_vpAudPcm); g_vp.pcmRate = g_vpAudRate; g_vp.audioDecoded = true; }
            else g_vp.audioWanted = false;
            g_vp.audioDecoding = false; g_vpAudDone = false; g_vpAudPcm.clear();
        }
    }
    // segments: sequential download, kept compressed
    if (!g_vp.allSegs) {
        if (!g_vp.segReqActive && g_vp.segNext < g_vp.nSeg) {
            std::string url = g_vp.segBase + std::to_string(g_vp.segNext) + ".b64";
            if (StartGet(url, g_vp.segReq)) g_vp.segReqActive = true;
        }
        if (g_vp.segReqActive && ReqDone(g_vp.segReq)) {
            if (ReqOk(g_vp.segReq.req)) {
                std::vector<uint8_t> blob = B64Decode(ReqText(g_vp.segReq.req));
                int n = (blob.size() >= 10 && memcmp(blob.data(), "BSCR", 4) == 0) ? (blob[8] | (blob[9] << 8)) : 0;
                if (n > 0) { g_vp.segBlobs.push_back(std::move(blob)); g_vp.segNext++; }
                else { BNM_FileCheckpointF("Video: segment %d bad -> abort", g_vp.segNext); ReqDispose(g_vp.segReq); g_vp.segReqActive = false; StopVideo(); return; }
                if (g_vp.segNext >= g_vp.nSeg) g_vp.allSegs = true;
            } else { BNM_FileCheckpointF("Video: segment %d download FAILED -> abort", g_vp.segNext); ReqDispose(g_vp.segReq); g_vp.segReqActive = false; StopVideo(); return; }
            ReqDispose(g_vp.segReq); g_vp.segReqActive = false;
        }
    }
    // build as soon as segment 0 is in and audio is ready (or gave up / timed out);
    // later segments keep streaming while it plays
    bool audioReady = !g_vp.audioWanted || g_vp.audioDecoded;
    if (!audioReady && !g_vp.segBlobs.empty() && (now - g_vp.loadStart) > 15.0) { g_vp.audioWanted = false; audioReady = true; }
    if (!g_vp.built && !g_vp.segBlobs.empty() && g_vp.totalFrames > 0 && audioReady) VideoBuild(now);
}
static void VideoDisplayTick(double now) {
    Il2CppObject* go = g_vp.go && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.go) : nullptr;
    if (!go || !Alive(go)) { VideoFreeObjects(); g_vp.built = false; g_vp.startAt = now; return; }   // room reload destroyed it -> rebuild
    double t;
    Il2CppObject* src = g_vp.audioSrc && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_vp.audioSrc) : nullptr;
    if (src && Alive(src)) { try { t = (double)Method<float>(Class("UnityEngine", "AudioSource").GetMethod("get_time", 0))[src](); } catch (...) { t = now - g_vp.startAt; } }
    else t = now - g_vp.startAt;
    if (g_vp.totalFrames > 0) {
        int want = (int)(t * g_vp.fps);
        if (g_vp.loop) { want %= g_vp.totalFrames; if (want < 0) want += g_vp.totalFrames; }
        else if (want >= g_vp.totalFrames) want = g_vp.totalFrames - 1;
        int seg = want / g_vp.segFrames, local = want % g_vp.segFrames;
        // a segment that hasn't streamed in yet: hold the current frame
        if (want != g_vp.curFrame && seg < (int)g_vp.segBlobs.size()) {
            if (seg != g_vp.cacheSeg) {
                DecodedScreen d;
                if (DecodeScreen(g_vp.segBlobs[seg], d)) { g_vp.cache = std::move(d); g_vp.cacheSeg = seg; }
            }
            if (g_vp.cacheSeg == seg && local < g_vp.cache.n) {
                try { VideoUploadFrame(local); } catch (...) {}
                g_vp.curFrame = want;
            }
        }
    }
    FollowHead(go);
}
// 131b9: dedicated 3 s poll of the active video so Load/Stop land within
// seconds (the general feed is 30 s). Body is one V2 line or empty (= stop):
//   V2 id WxHxFPS nSeg segFrames frames loop audioUrl segBase
// A FAILED request changes nothing (only a real empty answer stops the video).
// video URL comes from the kit settings: KitUrl("/client/video.txt")
static PendingReq g_vidPollReq;
static double g_nextVidPollAt = 0;
static void ParseVideoState(const std::string& body) {
    VideoWanted vw; bool seen = false;
    size_t pos = 0;
    while (pos < body.size() && !seen) {
        size_t nl = body.find('\n', pos);
        std::string line = body.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? body.size() : nl + 1;
        if (line.rfind("V2\t", 0) != 0) continue;
        std::vector<std::string> g; size_t a = 0, b;
        while ((b = line.find('\t', a)) != std::string::npos) { g.push_back(line.substr(a, b - a)); a = b + 1; }
        g.push_back(line.substr(a));
        if (g.size() < 9) continue;
        int w = 0, h = 0, fps = 10; sscanf(g[2].c_str(), "%dx%dx%d", &w, &h, &fps);
        vw.id = g[1]; vw.w = w; vw.h = h; vw.fps = fps > 0 ? fps : 10;
        vw.nSeg = atoi(g[3].c_str()); vw.segFrames = atoi(g[4].c_str()); vw.frames = atoi(g[5].c_str());
        vw.loop = (g[6] != "0"); vw.audioUrl = g[7]; vw.segBase = g[8];
        seen = vw.w > 0 && vw.h > 0 && vw.nSeg > 0;
    }
    std::lock_guard<std::mutex> lk(g_vidReqMtx);
    g_vidWanted = seen ? vw : VideoWanted{};
    g_vidWantedSet = true;
}
static void VideoTick(double now) {
    if (g_vidPollReq.req) {
        if (ReqDone(g_vidPollReq)) {
            if (ReqOk(g_vidPollReq.req)) { try { ParseVideoState(ReqText(g_vidPollReq.req)); } catch (...) {} }
            ReqDispose(g_vidPollReq);
        }
    } else if (now >= g_nextVidPollAt) {
        g_nextVidPollAt = now + 3.0;
        StartGet(KitUrl("/client/video.txt"), g_vidPollReq);
    }
    VideoWanted w; bool set;
    { std::lock_guard<std::mutex> lk(g_vidReqMtx); w = g_vidWanted; set = g_vidWantedSet; }
    if (set && w.id != g_vp.id) {
        if (w.id.empty()) StopVideo();
        else StartVideoLoad(w);
    }
    if (g_vp.id.empty()) return;
    VideoPump(now);                                  // keeps streaming segments after playback starts
    if (g_vp.built && !g_vp.id.empty()) VideoDisplayTick(now);
}

static void RemoteTick() {
    double now = nowSeconds();
    static bool firstTick = true;
    if (firstTick) {
        firstTick = false; RLOG("Remote: first tick");
        InstallCrashRecorder();
        BNM_FileCheckpointF("Crash recorder installed (fd=%d)", g_crashFd);
    }
    // feed poll
    if (g_feedReq.req) {
        if (ReqDone(g_feedReq)) {
            if (ReqOk(g_feedReq.req)) {
                try { std::string body = ReqText(g_feedReq.req); RLOG("Remote: feed ok (%d bytes)", (int)body.size()); ParseFeed(body); }
                catch (...) { RLOG("Remote: feed parse EXCEPTION"); }
            } else RLOG("Remote: feed request failed");
            ReqDispose(g_feedReq);
        }
    } else if (now >= g_nextFeedAt) {
        g_nextFeedAt = now + 30.0;
        if (!StartGet(KitUrl("/client/feed.txt"), g_feedReq)) RLOG("Remote: could not start feed request");
    }
    // disk downloads -> decode on a worker thread
    for (size_t i = 0; i < g_audioReqs.size();) {
        auto& p = g_audioReqs[i];
        if (!ReqDone(p)) { i++; continue; }
        if (ReqOk(p.req)) {
            std::string b64 = ReqText(p.req);
            std::string clip = p.clip, ver = p.ver;
            std::thread([b64, clip, ver]() {
                DecodedDisk d; d.clip = clip; d.ver = ver;
                if (DecodeToMono(B64Decode(b64), d.pcm, d.rate)) {
                    std::lock_guard<std::mutex> lk(g_decMtx); g_decoded.push_back(std::move(d));
                } else BNM_FileCheckpointF("Remote: disk %s decode failed", clip.c_str());
            }).detach();
        } else RLOG("Remote: disk %s download failed", p.clip.c_str());
        ReqDispose(p);
        g_audioReqs.erase(g_audioReqs.begin() + i);
    }
    {
        std::vector<DecodedDisk> ready;
        { std::lock_guard<std::mutex> lk(g_decMtx); ready.swap(g_decoded); }
        for (auto& d : ready) { try { BuildClipOnMain(d); } catch (...) { RLOG("Remote: build clip EXCEPTION"); } }
    }
    try { ScreensTick(now); } catch (...) { RLOG("Remote: screens tick EXCEPTION"); }
    try { VideoTick(now); } catch (...) { RLOG("Remote: video tick EXCEPTION"); }
    try { SyncFeedFriendsIntoGame(); } catch (...) {}
    // announcements
    Il2CppObject* go = g_annGO && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(g_annGO) : nullptr;
    if (go && Alive(go)) {
        if (now < g_annUntil) FollowHead(go);
        else {
            try { Method<void>(Class("UnityEngine", "Object").GetMethod("Destroy", 1))(go); } catch (...) {}
            Unpin(g_annGO); g_annGO = nullptr;
        }
    } else if (g_annGO) { Unpin(g_annGO); g_annGO = nullptr; }
    if (!g_annGO && !g_annQueue.empty()) {
        Ann a = g_annQueue.front(); g_annQueue.erase(g_annQueue.begin());
        ShowAnnouncement(a);
    }
}

static void (*orig_TickUpdate)(void*, void*) = nullptr;
// ---- Photon app ids from the kit settings --------------------------------
// The APK data still holds the app ids it was built with (PhotonServerSettings
// and the Photon Voice "VoiceAppSettings" asset). The kit replaces them with
// the host's ids: PUN's settings the first time the game reads them (the
// PhotonNetwork.PhotonServerSettings getter, before any connect), and the voice
// copies from the per-frame tick, because those load later. Ids are ALWAYS
// replaced: an empty id makes Photon refuse to connect instead of quietly using
// whoever's app the APK was built with.
static void (*p_wbarrier_set)(void*, void**, void*) = nullptr;
static Il2CppObject* KitGetObjField(Il2CppObject* obj, Class cls, const char* name) {
    if (!obj || !cls.IsValid()) return nullptr;
    FieldBase f = cls.GetField(name);
    if (!f.IsValid() || !f.GetInfo()) return nullptr;
    return *(Il2CppObject**)((uint8_t*)obj + f.GetInfo()->offset);
}
static bool KitSetObjField(Il2CppObject* obj, Class cls, const char* name, Il2CppObject* val) {
    if (!obj || !cls.IsValid()) return false;
    FieldBase f = cls.GetField(name);
    if (!f.IsValid() || !f.GetInfo()) return false;
    void** slot = (void**)((uint8_t*)obj + f.GetInfo()->offset);
    if (p_wbarrier_set) p_wbarrier_set(obj, slot, val); else *slot = val;
    return true;
}
static void KitApplyAppSettings(Il2CppObject* appSettings, const char* what) {
    if (!appSettings) return;
    Class as("Photon.Realtime", "AppSettings");
    std::string rt = KitCfg("photon_app_id"), vo = KitCfg("photon_voice_app_id"), rg = KitCfg("photon_region");
    bool ok = KitSetObjField(appSettings, as, "AppIdRealtime", (Il2CppObject*)CreateMonoString(rt.c_str()));
    ok = KitSetObjField(appSettings, as, "AppIdVoice", (Il2CppObject*)CreateMonoString(vo.c_str())) && ok;
    if (!rg.empty()) KitSetObjField(appSettings, as, "FixedRegion", (Il2CppObject*)CreateMonoString(rg.c_str()));
    BNM_FileCheckpointF("Kit: Photon ids -> %s (%s)", what, ok ? "ok" : "FIELDS NOT FOUND");
}
static std::atomic<bool> g_kitPunApplied{false};
static void* (*orig_PN_GetSettings)(void*) = nullptr;
static void* hook_PN_GetSettings(void* mi) {
    void* ss = orig_PN_GetSettings ? orig_PN_GetSettings(mi) : nullptr;
    if (ss && !g_kitPunApplied.exchange(true)) {
        try { KitApplyAppSettings(KitGetObjField((Il2CppObject*)ss, Class("Photon.Pun", "ServerSettings"), "AppSettings"),
                                  "PhotonServerSettings"); }
        catch (...) { BNM_FileCheckpoint("Kit: Photon ids EXCEPTION (PUN)"); }
    }
    return ss;
}
// Second layer: every Photon connection (PUN and Voice both go through
// LoadBalancingClient.ConnectUsingSettings(AppSettings)) gets the host's ids
// written into the settings it is about to use, right before connecting. The
// client picks AppIdRealtime or AppIdVoice by its own type, so both are set.
static bool (*orig_LBC_Connect)(void*, void*, void*) = nullptr;
static bool hook_LBC_Connect(void* self, void* appSettings, void* mi) {
    try { KitApplyAppSettings((Il2CppObject*)appSettings, "LoadBalancingClient.ConnectUsingSettings"); }
    catch (...) { BNM_FileCheckpoint("Kit: Photon ids EXCEPTION (connect)"); }
    return orig_LBC_Connect ? orig_LBC_Connect(self, appSettings, mi) : false;
}
static std::set<void*> g_kitVoiceDone;
static double g_kitVoiceNext = 0;
static void KitPhotonTick() {
    double now = nowSeconds();
    if (now < g_kitVoiceNext) return;
    g_kitVoiceNext = now + 2.0;
    Method<Il2CppObject*> findAll(FindOverload(Class("UnityEngine", "Resources"), "FindObjectsOfTypeAll", {"System.Type"}));
    if (!findAll.IsValid()) return;
    struct Src { const char* ns; const char* cls; const char* field; } srcs[] = {
        {"Photon.Voice", "PhotonAppSettings", "AppSettings"},      // the VoiceAppSettings asset
        {"Photon.Voice.Unity", "VoiceConnection", "Settings"},     // a voice connection's own copy
    };
    for (auto& sdef : srcs) {
        Class c(sdef.ns, sdef.cls);
        if (!c.IsValid()) continue;
        auto* arr = (uint8_t*)findAll((Il2CppObject*)c.GetMonoType());
        if (!arr) continue;
        uintptr_t n = *(uintptr_t*)(arr + 0x18);
        for (uintptr_t i = 0; i < n; i++) {
            void* o = ((void**)(arr + 0x20))[i];
            if (!o || g_kitVoiceDone.count(o)) continue;
            g_kitVoiceDone.insert(o);
            KitApplyAppSettings(KitGetObjField((Il2CppObject*)o, c, sdef.field), sdef.cls);
        }
    }
}
static void InstallKitPhoton() {
    try {
        p_wbarrier_set = (void(*)(void*, void**, void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gc_wbarrier_set_field");
        Class pn("Photon.Pun", "PhotonNetwork");
        MethodBase g = pn.IsValid() ? pn.GetMethod("get_PhotonServerSettings", 0) : MethodBase();
        if (g.IsValid()) { BasicHook(g, hook_PN_GetSettings, orig_PN_GetSettings); BNM_FileCheckpoint("Kit: PhotonServerSettings getter hooked"); }
        else BNM_FileCheckpoint("Kit: PhotonNetwork.get_PhotonServerSettings NOT FOUND");
        Class lbc("Photon.Realtime", "LoadBalancingClient");
        MethodBase c = lbc.IsValid() ? FindOverload(lbc, "ConnectUsingSettings", {"Photon.Realtime.AppSettings"}) : MethodBase();
        if (c.IsValid()) { BasicHook(c, hook_LBC_Connect, orig_LBC_Connect); BNM_FileCheckpoint("Kit: LoadBalancingClient.ConnectUsingSettings hooked"); }
        else BNM_FileCheckpoint("Kit: LoadBalancingClient.ConnectUsingSettings NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("Kit: EXCEPTION installing Photon hook"); }
}

static void hook_TickUpdate(void* self, void* mi) {
    if (orig_TickUpdate) orig_TickUpdate(self, mi);
    try { RemoteTick(); } catch (...) {}
    try { KitPhotonTick(); } catch (...) {}
}

// AudioSource.Play(): if the clip about to play is a remotely replaced disk
// track, play the downloaded clip instead.
static void (*orig_AS_Play)(void*, void*) = nullptr;
static void hook_AS_Play(void* self, void* mi) {
    try {
        bool any; { std::lock_guard<std::mutex> lk(g_diskMtx); any = !g_diskClipHandle.empty(); }
        if (any && self) {
            Class as("UnityEngine", "AudioSource");
            Il2CppObject* clip = Method<Il2CppObject*>(as.GetMethod("get_clip", 0))[(Il2CppObject*)self]();
            if (clip) {
                std::string name;
                auto it = g_clipNameCache.find(clip);
                if (it != g_clipNameCache.end()) name = it->second;
                else {
                    name = MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[clip]());
                    g_clipNameCache[clip] = name;
                }
                void* h = nullptr;
                { std::lock_guard<std::mutex> lk(g_diskMtx); auto d = g_diskClipHandle.find(name); if (d != g_diskClipHandle.end()) h = d->second; }
                Il2CppObject* repl = h && p_gchandle_target ? (Il2CppObject*)p_gchandle_target(h) : nullptr;
                if (repl && repl != clip) Method<void>(as.GetMethod("set_clip", 1))[(Il2CppObject*)self](repl);
            }
        }
    } catch (...) {}
    if (orig_AS_Play) orig_AS_Play(self, mi);
}

static void InstallRemoteClient() {
    try {
        p_gchandle_new = (void*(*)(void*, bool))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_new");
        p_gchandle_target = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_get_target");
        p_gchandle_free = (void(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_gchandle_free");
        p_object_new = (void*(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_object_new");
        p_array_new = (void*(*)(void*, uintptr_t))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_array_new");
        Class aom("", "AccountOnlineManager", Image("Assembly-CSharp.dll"));
        MethodBase up = aom.IsValid() ? aom.GetMethod("Update", 0) : MethodBase();
        if (up.IsValid()) { BasicHook(up, hook_TickUpdate, orig_TickUpdate); BNM_FileCheckpoint("Remote: tick hooked (AccountOnlineManager.Update)"); }
        else BNM_FileCheckpoint("Remote: AccountOnlineManager.Update NOT FOUND");
        Class fmgr("", "FriendManager", Image("Assembly-CSharp.dll"));
        MethodBase isf = fmgr.IsValid() ? FindOverload(fmgr, "IsFriend", {"System.String"}) : MethodBase();
        if (isf.IsValid()) { BasicHook(isf, hook_FM_IsFriend, orig_FM_IsFriend); BNM_FileCheckpoint("Friends: IsFriend hooked"); }
        else BNM_FileCheckpoint("Friends: IsFriend NOT FOUND");
        Class mapObj("", "MapObject", Image("Assembly-CSharp.dll"));
        MethodBase ssm = mapObj.IsValid() ? FindOverload(mapObj, "SetSharedMaterials", {"UnityEngine.Material[]"}) : MethodBase();
        if (ssm.IsValid()) { BasicHook(ssm, hook_MO_SetSharedMaterials, orig_MO_SetSharedMaterials); BNM_FileCheckpoint("Remote: MapObject.SetSharedMaterials hooked"); }
        else BNM_FileCheckpoint("Remote: MapObject.SetSharedMaterials NOT FOUND");
        MethodBase play = Class("UnityEngine", "AudioSource").GetMethod("Play", 0);
        if (play.IsValid()) { BasicHook(play, hook_AS_Play, orig_AS_Play); BNM_FileCheckpoint("Remote: AudioSource.Play hooked"); }
        else BNM_FileCheckpoint("Remote: AudioSource.Play NOT FOUND");
        BNM_FileCheckpointF("Remote: gchandle api %d/%d/%d", p_gchandle_new != nullptr, p_gchandle_target != nullptr, p_gchandle_free != nullptr);
    } catch (...) { BNM_FileCheckpoint("Remote: EXCEPTION installing"); }
}

// =====================================================================
//  Item dispenser accepts EVERYTHING. The dispenser block
//  (CustomWirableDispenserMapObject, prefab "itemDispenser") takes items
//  through child ItemDespawnZones (ItemInput / PinInput / IngredientInput /
//  MobSpawnEggInput); the zone filter refuses many items before the
//  dispenser ever sees them. For those zones only, override the filter.
//  Other despawn zones (trash, cauldrons, ...) keep their normal rules.
// =====================================================================
static std::mutex g_zoneMtx;
static std::map<void*, bool> g_isDispenserZone;
static bool IsDispenserInputZone(void* zone) {
    {
        std::lock_guard<std::mutex> lk(g_zoneMtx);
        auto it = g_isDispenserZone.find(zone);
        if (it != g_isDispenserZone.end()) return it->second;
    }
    bool yes = false;
    try {
        Method<Il2CppObject*> gname(Class("UnityEngine", "Object").GetMethod("get_name", 0));
        Il2CppObject* go = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[(Il2CppObject*)zone]();
        std::string zn = go ? MonoStr(gname[go]()) : "";
        if (zn == "ItemInput" || zn == "PinInput" || zn == "IngredientInput" || zn == "MobSpawnEggInput") {
            Class tr("UnityEngine", "Transform");
            Il2CppObject* t = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_transform", 0))[(Il2CppObject*)zone]();
            for (int depth = 0; t && depth < 3 && !yes; depth++) {
                t = Method<Il2CppObject*>(tr.GetMethod("get_parent", 0))[t]();
                if (!t) break;
                Il2CppObject* pgo = Method<Il2CppObject*>(Class("UnityEngine", "Component").GetMethod("get_gameObject", 0))[t]();
                if (pgo && MonoStr(gname[pgo]()).find("itemDispenser") != std::string::npos) yes = true;
            }
        }
    } catch (...) {}
    std::lock_guard<std::mutex> lk(g_zoneMtx);
    g_isDispenserZone[zone] = yes;
    return yes;
}
static std::atomic<int> g_dispLogN{0};
static uint64_t (*orig_ZoneCanAccept)(void*, void*, void*) = nullptr;
static uint64_t hook_ZoneCanAccept(void* self, void* item, void* mi) {
    uint64_t r = orig_ZoneCanAccept ? orig_ZoneCanAccept(self, item, mi) : 0;
    if ((r & 1) == 0 && self && IsDispenserInputZone(self)) {
        if (g_dispLogN.fetch_add(1) < 50) {
            std::string in;
            try { in = item ? MonoStr(Method<Il2CppObject*>(Class("UnityEngine", "Object").GetMethod("get_name", 0))[(Il2CppObject*)item]()) : "(null)"; } catch (...) {}
            BNM_FileCheckpointF("Dispenser: allowing refused item '%s'", in.c_str());
        }
        return 1;
    }
    return r;
}
static uint64_t (*orig_ZoneIgnore)(void*, void*, void*) = nullptr;
static uint64_t hook_ZoneIgnore(void* self, void* item, void* mi) {
    uint64_t r = orig_ZoneIgnore ? orig_ZoneIgnore(self, item, mi) : 0;
    if ((r & 1) && self && IsDispenserInputZone(self)) return 0;
    return r;
}
static uint64_t (*orig_ZoneAllowsLoaded)(void*, void*) = nullptr;
static uint64_t hook_ZoneAllowsLoaded(void* self, void* mi) {
    uint64_t r = orig_ZoneAllowsLoaded ? orig_ZoneAllowsLoaded(self, mi) : 0;
    if ((r & 1) == 0 && self && IsDispenserInputZone(self)) return 1;
    return r;
}
// The zone filter was not the real gate: the dispenser's item handler calls
// static CustomWirableDispenserMapObject.TryProcessInputItem(item, inputType,
// out key, out internalState[], out errorMessage, out bool) and shows
// "This item can't be used in a dispenser" when it returns false. Force it to
// accept every item, storing the item's own key (Item+0x60) so the dispenser
// can spawn it again.
static std::atomic<int> g_tpiLogN{0};
static bool (*orig_TryProcessInputItem)(void*, void*, void**, void**, void**, bool*, void*) = nullptr;
static bool hook_TryProcessInputItem(void* item, void* inType, void** keyOut, void** stateOut,
                                     void** errOut, bool* errFlag, void* mi) {
    bool ok = orig_TryProcessInputItem ? orig_TryProcessInputItem(item, inType, keyOut, stateOut, errOut, errFlag, mi) : false;
    if (ok || !item || !keyOut || !stateOut) return ok;
    std::string key, err;
    void* k = nullptr;
    try {
        k = *(void**)((uint8_t*)item + 0x60);
        key = MonoStr(k);
        err = errOut ? MonoStr(*errOut) : "";
    } catch (...) {}
    bool isBlock = key == "mapObject" || key.rfind("mapObject:", 0) == 0;
    if (g_tpiLogN.fetch_add(1) < 40)
        BNM_FileCheckpointF("Dispenser: game refused '%s' (type=%s err='%s' flag=%d) -> %s",
                            key.c_str(), MonoStr(inType).c_str(), err.c_str(), errFlag ? (int)*errFlag : -1,
                            key.empty() ? "kept refused (no key)" : isBlock ? "kept refused (block)" : "accepting");
    if (key.empty()) return false;
    // Blocks ("mapObject") must NOT be accepted into a dispenser: the game can't
    // represent an arbitrary block in one and CRASHES when a map whose dispenser
    // holds a block loads. Only real items (grenades, tools, ingredients) belong,
    // so fall back to the game's own decision (refuse) for block items.
    if (key == "mapObject" || key.rfind("mapObject:", 0) == 0) return ok;
    *keyOut = k;
    if (!*stateOut && p_array_new) *stateOut = p_array_new(Class("System", "String").GetClass(), 0);
    if (errOut) *errOut = nullptr;
    if (errFlag) *errFlag = false;
    return true;
}
// IsItemAllowed(itemKey, itemParams[], ?, bool, inputType, out err, out key,
// out state[]) is the core check behind BOTH inserting an item
// (TryProcessInputItem) and validating a dispenser's saved contents when a map
// loads (ValidateInternalState) -- so forcing only the insert path let the
// items in, but the map load threw them away again. Accept here, keeping the
// item's own key + params (params carry e.g. which block / colour it is).
static std::atomic<int> g_iiaLogN{0};
static bool (*orig_IsItemAllowed)(void*, void*, int, bool, void*, void**, void**, void**, void*) = nullptr;
static bool hook_IsItemAllowed(void* key, void* params, int e, bool b, void* inType,
                               void** errOut, void** keyOut, void** stateOut, void* mi) {
    bool ok = orig_IsItemAllowed ? orig_IsItemAllowed(key, params, e, b, inType, errOut, keyOut, stateOut, mi) : false;
    if (ok || !key || !keyOut || !stateOut) return ok;
    if (MonoStr(key).empty()) return false;
    // Same block guard as TryProcessInputItem. This path ALSO runs when a map
    // loads (ValidateInternalState): refusing a block here makes the dispenser
    // DROP the saved block instead of crashing, so already-broken maps load.
    { std::string kk = MonoStr(key);
      if (kk == "mapObject" || kk.rfind("mapObject:", 0) == 0) return ok; }
    if (g_iiaLogN.fetch_add(1) < 30)
        BNM_FileCheckpointF("Dispenser: IsItemAllowed refused '%s' (err='%s') -> accepting",
                            MonoStr(key).c_str(), errOut ? MonoStr(*errOut).c_str() : "");
    *keyOut = key;
    *stateOut = params ? params : (p_array_new ? p_array_new(Class("System", "String").GetClass(), 0) : nullptr);
    if (errOut) *errOut = nullptr;
    return true;
}
// ---- IsItemAllowed v2 (131b11): register-exact forwarding ---------------
// v1 declared a GUESSED signature (int/bool at slots 2/3, static) and so
// truncated/shifted the real arguments -> the original crashed on its first
// call. v2 takes x0-x7 + 4 stack slots as raw words and forwards them
// untouched, so the original always sees exactly what the caller passed. The
// accept-all only runs when the real parameter layout (read from MethodInfo at
// install: static/instance, byref outs, no float/struct args) was understood;
// otherwise it just forwards (items then aren't kept on map load, but no crash).
typedef uintptr_t (*IIAFn)(uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t,
                           uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t, uintptr_t);
static IIAFn orig_IIA2 = nullptr;
static int g_iiaKey = -1, g_iiaParams = -1, g_iiaErr = -1, g_iiaKeyOut = -1, g_iiaStateOut = -1;
static bool g_iiaForce = false;
static std::atomic<int> g_iia2LogN{0};
static uintptr_t hook_IIA2(uintptr_t a0, uintptr_t a1, uintptr_t a2, uintptr_t a3, uintptr_t a4, uintptr_t a5,
                           uintptr_t a6, uintptr_t a7, uintptr_t a8, uintptr_t a9, uintptr_t a10, uintptr_t a11) {
    uintptr_t r = orig_IIA2(a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11);
    if ((r & 0xFF) || !g_iiaForce) return r;
    uintptr_t a[12] = {a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11};
    void* key = (void*)a[g_iiaKey];
    void** keyOut = (void**)a[g_iiaKeyOut];
    void** stateOut = (void**)a[g_iiaStateOut];
    if (!key || !keyOut || !stateOut) return r;
    std::string ks;
    try { ks = MonoStr(key); } catch (...) { return r; }
    // blocks ("mapObject") stay refused -- the dispenser can't represent them
    if (ks.empty() || ks == "mapObject" || ks.rfind("mapObject:", 0) == 0) return r;
    if (g_iia2LogN.fetch_add(1) < 30) BNM_FileCheckpointF("Dispenser: IsItemAllowed refused '%s' -> accepting", ks.c_str());
    *keyOut = key;
    void* params = g_iiaParams >= 0 ? (void*)a[g_iiaParams] : nullptr;
    *stateOut = params ? params : (p_array_new ? p_array_new(Class("System", "String").GetClass(), 0) : nullptr);
    if (g_iiaErr >= 0 && a[g_iiaErr]) *(void**)a[g_iiaErr] = nullptr;
    return 1;
}
static void InstallIsItemAllowedV2(MethodBase iia) {
    auto* mi = iia.GetInfo();
    if (!mi) return;
    static void* (*p_class_from_type)(const Il2CppType*) = nullptr;
    static bool (*p_class_is_enum)(void*) = nullptr;
    if (!p_class_from_type) p_class_from_type = (void*(*)(const Il2CppType*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_from_type");
    if (!p_class_is_enum) p_class_is_enum = (bool(*)(void*))BNM_dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_is_enum");
    bool isStatic = (mi->flags & 0x0010) != 0, ok = true;
    int reg = isStatic ? 0 : 1, nOutStr = 0;
    std::string sig = isStatic ? "static " : "instance ";
    for (int pi = 0; mi->parameters && pi < (int)mi->parameters_count; pi++) {
        const Il2CppType* t = mi->parameters[pi];
        if (!t) { ok = false; break; }
        sig += std::string(pi ? ", " : "(") + (t->byref ? "out " : "") + typeName(t);
        if (!t->byref) {
            if (t->type == IL2CPP_TYPE_R4 || t->type == IL2CPP_TYPE_R8) ok = false;                 // float -> v-reg
            if (t->type == IL2CPP_TYPE_VALUETYPE || t->type == IL2CPP_TYPE_GENERICINST) {
                void* k = p_class_from_type ? p_class_from_type(t) : nullptr;
                if (!k || !(p_class_is_enum && p_class_is_enum(k))) ok = false;                   // struct: unknown size
            }
            if (t->type == IL2CPP_TYPE_STRING && g_iiaKey < 0) g_iiaKey = reg;
            if (t->type == IL2CPP_TYPE_SZARRAY && g_iiaParams < 0) g_iiaParams = reg;
        } else {
            if (t->type == IL2CPP_TYPE_STRING) { if (nOutStr++ == 0) g_iiaErr = reg; else g_iiaKeyOut = reg; }
            if (t->type == IL2CPP_TYPE_SZARRAY) g_iiaStateOut = reg;
        }
        reg++;
    }
    sig += ")";
    if (reg + 1 > 12) ok = false;                                  // args + MethodInfo must fit 12 slots
    g_iiaForce = ok && g_iiaKey >= 0 && g_iiaKeyOut >= 0 && g_iiaStateOut >= 0;
    BasicHook(iia, hook_IIA2, orig_IIA2);
    BNM_FileCheckpointF("Dispenser: IsItemAllowed v2 hooked %s -> key@%d params@%d err@%d keyOut@%d stateOut@%d accept-all=%d",
                        sig.c_str(), g_iiaKey, g_iiaParams, g_iiaErr, g_iiaKeyOut, g_iiaStateOut, (int)g_iiaForce);
}

// ---- News board buttons (131b11) ---------------------------------------
// ActiveNewsDisplay's UnityEvent targets (method names are real: UnityEvents
// call by name). Discord -> our server's invite; "Leave a review" -> the built-in
// mod menu (BNM Explorer web UI served on the headset at :8080; the label text
// is changed in the game data).
static const char* kDefaultDiscord = "https://discord.gg/VhFmdAyXGk";   // Blaze's Discord
static const char* kModMenuUrl = "http://localhost:8080";
static void OpenUrl(const char* url) {
    try {
        Method<void> o(FindOverload(Class("UnityEngine", "Application"), "OpenURL", {"System.String"}));
        if (o.IsValid()) o(CreateMonoString(url));
        BNM_FileCheckpointF("Buttons: OpenURL %s (%d)", url, (int)o.IsValid());
    } catch (...) { BNM_FileCheckpoint("Buttons: OpenURL EXCEPTION"); }
}
static void (*orig_OpenDiscord)(void*, void*) = nullptr;
static void hook_OpenDiscord(void*, void*) { OpenUrl(KitCfg("discord", kDefaultDiscord).c_str()); }
static void (*orig_OpenReview)(void*, void*) = nullptr;
static void hook_OpenReview(void*, void*) { OpenUrl(kModMenuUrl); }
static void InstallNewsButtons() {
    try {
        Class nd("", "ActiveNewsDisplay", Image("Assembly-CSharp.dll"));
        MethodBase d = nd.IsValid() ? nd.GetMethod("OnOpenDiscordLinkButtonPressed", 0) : MethodBase();
        MethodBase r = nd.IsValid() ? nd.GetMethod("OnLeaveReviewLinkButtonPressed", 0) : MethodBase();
        if (d.IsValid()) BasicHook(d, hook_OpenDiscord, orig_OpenDiscord);
        if (r.IsValid()) BasicHook(r, hook_OpenReview, orig_OpenReview);
        BNM_FileCheckpointF("Buttons: discord hook=%d review->modmenu hook=%d", d.IsValid(), r.IsValid());
    } catch (...) { BNM_FileCheckpoint("Buttons: EXCEPTION installing"); }
}

// ---- BLAZE role blocks: MapObject.requiredRoleKey = "blaze" -------------
// MapObject.CanUseMapObject(roles) checks requiredRoleKey against a static
// NetworkManager list that this server never fills (it's empty for everyone),
// so even blaze holders were refused. For "blaze" blocks, also accept when the
// local account's own roleKeys (AccountData, from the server login) contain
// "blaze". The server only sends that role to people who have it.
static double g_blazeRoleCheckedAt = -100;
static bool g_hasBlazeRole = false;
static bool LocalHasRole(const char* role) {
    double now = nowSeconds();
    if (now - g_blazeRoleCheckedAt < 5.0) return g_hasBlazeRole;
    g_blazeRoleCheckedAt = now;
    bool found = false;
    try {
        Class am("", "AccountManager", Image("Assembly-CSharp.dll"));
        Il2CppObject* ad = Method<Il2CppObject*>(am.GetMethod("get_accountData", 0))();
        if (ad) {
            Il2CppClass* strK = Class("System", "String").GetClass();
            for (Class c(ad->klass); c.IsValid() && c.GetClass() && !found; c = c.GetParent()) {
                for (auto& fb : c.GetFields(false)) {
                    auto* fi = fb.GetInfo();
                    if (!fi || !fi->type || (fi->type->attrs & 0x10 /* static */)) continue;
                    if (typeName(fi->type).rfind("System.Collections.Generic.List", 0) != 0) continue;
                    auto* list = *(uint8_t**)((uint8_t*)ad + fi->offset);
                    if (!list) continue;
                    auto* items = *(uint8_t**)(list + 0x10);
                    int size = *(int*)(list + 0x18);
                    if (!items || size <= 0 || size > 4096) continue;
                    // Only List<string>: other lists (e.g. of ints/structs) hold
                    // raw values, and reading those as object pointers crashed.
                    auto* arrK = ((Il2CppObject*)items)->klass;
                    if (!arrK || arrK->element_class != strK) continue;
                    for (int i = 0; i < size && !found; i++) {
                        auto* e = ((Il2CppObject**)(items + 0x20))[i];
                        if (e && e->klass == strK && MonoStr(e) == role) found = true;
                    }
                }
                if (strcmp(c.GetClass()->name, "Object") == 0) break;
            }
        }
    } catch (...) {}
    g_hasBlazeRole = found;
    return found;
}
static std::atomic<int> g_cumLogN{0};
static bool (*orig_CanUseMapObject)(void*, void*, void*) = nullptr;
static bool hook_CanUseMapObject(void* self, void* roles, void* mi) {
    bool ok = orig_CanUseMapObject ? orig_CanUseMapObject(self, roles, mi) : true;
    if (ok || !self) return ok;
    std::string req;
    try { req = MonoStr(*(void**)((uint8_t*)self + 0x68)); } catch (...) {}
    bool allow = (req == "blaze") && LocalHasRole("blaze");
    if (g_cumLogN.fetch_add(1) < 20)
        BNM_FileCheckpointF("Roles: CanUseMapObject requires '%s' -> local blaze=%d -> %s",
                            req.c_str(), (int)g_hasBlazeRole, allow ? "ALLOW" : "refuse");
    return allow;
}
static void InstallBlazeRoleCheck() {
    try {
        Class mo("", "MapObject", Image("Assembly-CSharp.dll"));
        MethodBase m = mo.IsValid() ? mo.GetMethod("CanUseMapObject", 1) : MethodBase();
        if (m.IsValid()) { BasicHook(m, hook_CanUseMapObject, orig_CanUseMapObject); BNM_FileCheckpoint("Roles: CanUseMapObject hooked"); }
        else BNM_FileCheckpoint("Roles: CanUseMapObject NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("Roles: EXCEPTION installing"); }
}

static void InstallDispenserAcceptAll() {
    try {
        // 131b9: the ItemDespawnZone hooks (CheckCanAcceptItem / CheckShouldIgnoreItem
        // / get_allowsLoadedItems) are NOT installed any more. They were the wrong
        // layer (see TryProcessInputItem below) and, worse, made a dispenser's
        // input zone swallow items the game deliberately ignores -- e.g. an item
        // still held or loaded into something -- so the dispenser despawned an
        // object that was still referenced -> native SIGSEGV on the main thread
        // ("put an item in a dispenser -> game crashed"). Accept-all lives only in
        // TryProcessInputItem + IsItemAllowed now; the zone uses vanilla rules.
        (void)hook_ZoneCanAccept; (void)hook_ZoneIgnore; (void)hook_ZoneAllowsLoaded;
        BNM_FileCheckpoint("Dispenser: zone hooks disabled (vanilla zone rules)");
        Class disp("", "CustomWirableDispenserMapObject", Image("Assembly-CSharp.dll"));
        // 131b10: IsItemAllowed is NOT hooked any more. The hook's guessed
        // signature (3rd param "int", 4th "bool") mis-forwarded the registers, so
        // the ORIGINAL crashed (SIGSEGV, junk fault address) on its first call --
        // i.e. on every dispenser insert AND on loading any map whose dispensers
        // hold items (the Starter House crashes). Crash recorder backtrace:
        // CRASH sig 11 -> libil2cpp -> hook_IsItemAllowed (orig call). Log the real
        // parameter types so it can be re-added correctly if ever needed.
        (void)hook_IsItemAllowed;
        // 131b11: re-hooked with register-exact forwarding (InstallIsItemAllowedV2).
        MethodBase iia = disp.IsValid() ? disp.GetMethod("IsItemAllowed", 8) : MethodBase();
        if (iia.IsValid()) { try { InstallIsItemAllowedV2(iia); } catch (...) { BNM_FileCheckpoint("Dispenser: IsItemAllowed v2 EXCEPTION"); } }
        else BNM_FileCheckpoint("Dispenser: IsItemAllowed NOT FOUND");
        MethodBase tpi = disp.IsValid() ? disp.GetMethod("TryProcessInputItem", 6) : MethodBase();
        if (tpi.IsValid()) { BasicHook(tpi, hook_TryProcessInputItem, orig_TryProcessInputItem); BNM_FileCheckpoint("Dispenser: TryProcessInputItem hooked"); }
        else BNM_FileCheckpoint("Dispenser: TryProcessInputItem NOT FOUND");
    } catch (...) { BNM_FileCheckpoint("Dispenser: EXCEPTION installing"); }
}

// ---------------------------------------------------------------------
//  Spawn direct (same fix as 1.19-blaze). A fresh install has not finished the
//  tutorial (a local pref), so the game spawns it in "tutorial" and then walks
//  it into the Starter House in the same session. That in-session room switch
//  with the full Starter House map breaks new players. Existing players spawn
//  straight into official_1 and load it fine. So the FIRST room of the session,
//  if it is "tutorial", becomes "official_1".
//
//  RoomManager's method names are obfuscated in 1.31, so GoToRoom is matched by
//  shape: static, first two params System.String (room key / room map key), at
//  most 7 params, every param passed in an integer register (reference, bool,
//  integer, enum). The hook forwards x0..x7 untouched apart from the room key.
// ---------------------------------------------------------------------
static std::atomic<bool> g_spawnDecided{false};
static const int kMaxSpawnHooks = 4;
static void* g_spawnOrig[kMaxSpawnHooks] = {};
static const char* g_spawnName[kMaxSpawnHooks] = {};
typedef uintptr_t (*Gpr8Fn)(void*, void*, void*, void*, void*, void*, void*, void*);

static void SpawnDirectCheck(int n, void** a0, void** a1) {
    if (g_spawnDecided.load()) return;
    try {
        std::string k0 = *a0 ? ((BNM::Structures::Mono::String*)*a0)->str() : "";
        std::string k1 = *a1 ? ((BNM::Structures::Mono::String*)*a1)->str() : "";
        if (k0.empty() && k1.empty()) return;
        g_spawnDecided = true;   // only the first room of the session
        void** key = k0 == "tutorial" ? a0 : (k1 == "tutorial" ? a1 : nullptr);
        if (key) *key = (void*)CreateMonoString("official_1");
        BNM_FileCheckpointF("SpawnDirect: first room via #%d (%s) args '%s','%s' -> %s", n,
                            g_spawnName[n] ? g_spawnName[n] : "?", k0.c_str(), k1.c_str(),
                            key ? "official_1" : "unchanged");
    } catch (...) { g_spawnDecided = true; }
}
template <int N>
static uintptr_t hook_SpawnRoom(void* a0, void* a1, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7) {
    SpawnDirectCheck(N, &a0, &a1);
    return ((Gpr8Fn)g_spawnOrig[N])(a0, a1, a2, a3, a4, a5, a6, a7);
}
static void* const kSpawnHooks[kMaxSpawnHooks] = {
    (void*)hook_SpawnRoom<0>, (void*)hook_SpawnRoom<1>, (void*)hook_SpawnRoom<2>, (void*)hook_SpawnRoom<3>};

static bool GprSafeParam(const Il2CppType* t) {
    static auto from_type = (void* (*)(const Il2CppType*))dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_from_type");
    static auto is_enum = (bool (*)(void*))dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_is_enum");
    static auto is_vt = (bool (*)(void*))dlsym(BNM::Internal::il2cppLibraryHandle, "il2cpp_class_is_valuetype");
    if (!t) return false;
    if (t->byref) return true;                       // ref/out -> pointer
    switch ((int)t->type) {
        case 0x02: case 0x03: case 0x04: case 0x05: case 0x06: case 0x07:
        case 0x08: case 0x09: case 0x0a: case 0x0b:   // bool, char, ints
        case 0x0e: case 0x12: case 0x1c: case 0x1d:   // string, class, object, array
        case 0x0f: case 0x18: case 0x19:              // ptr, IntPtr, UIntPtr
            return true;
        case 0x11: case 0x15: {                       // valuetype / generic inst
            if (!from_type || !is_enum || !is_vt) return false;
            void* k = from_type(t);
            if (!k) return false;
            if (is_enum(k)) return true;              // enums are integers
            return !is_vt(k);                         // generic reference type
        }
        default: return false;                        // float, struct, ...
    }
}

static void InstallSpawnDirect() {
    try {
        Class rm("", "RoomManager", Image("Assembly-CSharp.dll"));
        if (!rm.IsValid()) { BNM_FileCheckpoint("SpawnDirect: RoomManager NOT FOUND"); return; }
        int n = 0;
        for (auto& m : rm.GetMethods(false)) {
            auto* mi = m.GetInfo();
            if (!mi || !mi->methodPointer || !(mi->flags & 0x0010)) continue;   // static only
            int pc = mi->parameters_count;
            if (pc < 5 || pc > 7 || !mi->parameters) continue;
            if (typeName(mi->parameters[0]) != "System.String" || typeName(mi->parameters[1]) != "System.String") continue;
            bool ok = true;
            std::string sig;
            for (int i = 0; i < pc && ok; i++) {
                ok = GprSafeParam(mi->parameters[i]);
                sig += (i ? "," : "") + typeName(mi->parameters[i]);
            }
            if (!ok) { BNM_FileCheckpointF("SpawnDirect: skip %s(%s) (non-register param)", mi->name, sig.c_str()); continue; }
            if (n >= kMaxSpawnHooks) { BNM_FileCheckpointF("SpawnDirect: too many candidates, skip %s", mi->name); continue; }
            g_spawnName[n] = mi->name;
            BasicHook(m, kSpawnHooks[n], g_spawnOrig[n]);
            BNM_FileCheckpointF("SpawnDirect: hooked #%d %s(%s)", n, mi->name, sig.c_str());
            n++;
        }
        if (!n) BNM_FileCheckpoint("SpawnDirect: no GoToRoom-shaped method found");
    } catch (...) { BNM_FileCheckpoint("SpawnDirect: EXCEPTION"); }
}

static void OnLoaded() {
    // PUBLIC BUILD: only the BNM Explorer + its Debug.Log feed. The Meta
    // entitlement/user-proof bypass is installed from JNI_OnLoad
    // (OvrHookPollThread) and does not depend on il2cpp attach. All the
    // credential/crypto/backtrace diagnostics are removed for distribution.
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "OnLoaded fired (1.31.0) - il2cpp attach succeeded");
    BNM_FileCheckpoint("OnLoaded fired (1.31.0) - il2cpp attach succeeded");
    InstallGameAnalyticsBlock();           // kill third-party telemetry
    InstallVersionLabelClean();            // show just "1.31.0-blaze"
    ComputeBuildTag();                     // build id + native-lib hashes for the server
    ComputeDeviceKey();                    // headset key sent with every API call
    InstallPhotonAuthKey();                // ...and with every Photon room join
    InstallBackendRedirect();              // execute-api + lambda + dynamodb -> self-host
    InstallKitPhoton();                    // the host's Photon app ids (kit settings)
    InstallAccountValidateBypass();        // pass the client-side account integrity check
    InstallRemoteClient();                 // server announcements + remote disk audio
    InstallDispenserAcceptAll();           // item dispenser takes every item
    InstallBlazeRoleCheck();               // BLAZE blocks: placeable with the blaze role
    InstallSpawnDirect();                  // first room tutorial -> official_1 (new installs)
    InstallNewsButtons();                  // news board: Discord -> our invite, Review -> mod menu
    hookDebugLog();                        // feed Explorer /logs
    std::thread(startServer).detach();     // BNM Explorer on 0.0.0.0:8080
}

JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, [[maybe_unused]] void* reserved) {
    BNM_FileCheckpoint("=== JNI_OnLoad entered (1.31.0) ===");
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "JNI_OnLoad entered (1.31.0)");
    JNIEnv* env;
    g_vm = vm;
    vm->GetEnv((void**)&env, JNI_VERSION_1_6);
    LoadKitConfig();                       // kit settings: server, Photon ids, Discord
    std::thread(OvrHookPollThread).detach();
    BNM::Loading::AddOnLoadedEvent(OnLoaded);
    BNM::Loading::TryLoadByJNI(env);
    __android_log_print(ANDROID_LOG_ERROR, "MYDBG", "JNI_OnLoad: TryLoadByJNI returned (1.31.0)");
    BNM_FileCheckpoint("=== JNI_OnLoad: TryLoadByJNI returned (1.31.0) ===");
    return JNI_VERSION_1_6;
}
