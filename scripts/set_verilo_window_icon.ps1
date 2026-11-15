param(
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$BrowserProcessId,
    [Parameter(Mandatory = $true)][string]$IconPath
)

$ErrorActionPreference = 'Stop'

# The helper owns its HICONs for the lifetime of one explicitly named browser
# window. It never changes icons on other processes or destroys browser HICONs.
try {
    $veriloIconFile = (Get-Item -LiteralPath $IconPath -ErrorAction Stop).FullName
    if ([IO.Path]::GetExtension($veriloIconFile) -ne '.ico') {
        throw [ArgumentException]::new('ICO required.')
    }
    if (-not ('Verilo.NativeWindowIcon' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace Verilo
{
    public static class NativeWindowIcon
    {
        const uint WM_SETICON = 0x0080;
        const uint WM_GETICON = 0x007f;
        const uint IMAGE_ICON = 1;
        const uint LR_LOADFROMFILE = 0x0010;
        const uint SMTO_BLOCK_ABORTIFHUNG = 0x0003;

        delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetClassNameW(IntPtr hwnd, StringBuilder name, int capacity);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int capacity);
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll", SetLastError = true)]
        static extern uint GetDpiForWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        static extern int GetSystemMetricsForDpi(int index, uint dpi);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr LoadImageW(IntPtr instance, string name, uint type,
            int width, int height, uint flags);
        [DllImport("user32.dll", SetLastError = true)]
        static extern IntPtr SendMessageTimeoutW(IntPtr hwnd, uint message,
            IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool DestroyIcon(IntPtr icon);
        [DllImport("shell32.dll", PreserveSig = true)]
        static extern int SHGetPropertyStoreForWindow(IntPtr hwnd, ref Guid iid,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        struct PropertyKey
        {
            public Guid format;
            public uint id;
            public PropertyKey(Guid format, uint id) { this.format = format; this.id = id; }
        }

        // PROPVARIANT has an eight-byte header; LPWSTR is at offset eight on
        // both architectures. The largest native x64 variant occupies 24 bytes.
        [StructLayout(LayoutKind.Explicit, Size = 24)]
        struct PropVariant
        {
            [FieldOffset(0)] public ushort type;
            [FieldOffset(8)] public IntPtr text;
        }

        [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
            InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        interface IPropertyStore
        {
            [PreserveSig] int GetCount(out uint count);
            [PreserveSig] int GetAt(uint index, out PropertyKey key);
            [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
            [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
            [PreserveSig] int Commit();
        }

        sealed class IconPair
        {
            public IntPtr Small, Big;
            public int SmallWidth, SmallHeight, BigWidth, BigHeight;
        }

        static bool BelongsTo(IntPtr hwnd, int pid)
        {
            uint owner;
            GetWindowThreadProcessId(hwnd, out owner);
            return owner == (uint)pid;
        }

        static bool Matches(IntPtr hwnd, int pid)
        {
            if (!IsWindowVisible(hwnd) || !BelongsTo(hwnd, pid)) return false;
            var name = new StringBuilder(128);
            GetClassNameW(hwnd, name, name.Capacity);
            if (name.ToString() != "Chrome_WidgetWin_1") return false;
            var title = new StringBuilder(1024);
            GetWindowTextW(hwnd, title, title.Capacity);
            return title.ToString().IndexOf("Verilo", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        static IntPtr FindWindow(int pid)
        {
            IntPtr found = IntPtr.Zero;
            EnumWindowsProc callback = delegate(IntPtr hwnd, IntPtr ignored) {
                if (!Matches(hwnd, pid)) return true;
                found = hwnd;
                return false;
            };
            EnumWindows(callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            return found;
        }

        static bool Message(IntPtr hwnd, uint message, int kind, IntPtr value, out IntPtr previous)
        {
            UIntPtr result;
            var sent = SendMessageTimeoutW(hwnd, message, new IntPtr(kind), value,
                SMTO_BLOCK_ABORTIFHUNG, 500, out result);
            previous = unchecked((IntPtr)(long)result.ToUInt64());
            return sent != IntPtr.Zero;
        }

        static uint WindowDpi(IntPtr hwnd)
        {
            try { uint dpi = GetDpiForWindow(hwnd); return dpi == 0 ? 96u : dpi; }
            catch (EntryPointNotFoundException) { return 96u; }
        }

        static int Metric(int index, uint dpi, int baseline)
        {
            try { int size = GetSystemMetricsForDpi(index, dpi); if (size > 0) return size; }
            catch (EntryPointNotFoundException) { }
            return Math.Max(1, (int)Math.Round(baseline * dpi / 96.0));
        }

        static IconPair LoadIcons(string path, uint dpi)
        {
            var pair = new IconPair {
                SmallWidth = Metric(49, dpi, 16), SmallHeight = Metric(50, dpi, 16),
                BigWidth = Metric(11, dpi, 32), BigHeight = Metric(12, dpi, 32)
            };
            pair.Small = LoadImageW(IntPtr.Zero, path, IMAGE_ICON,
                pair.SmallWidth, pair.SmallHeight, LR_LOADFROMFILE);
            if (pair.Small == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            pair.Big = LoadImageW(IntPtr.Zero, path, IMAGE_ICON,
                pair.BigWidth, pair.BigHeight, LR_LOADFROMFILE);
            if (pair.Big == IntPtr.Zero) {
                int error = Marshal.GetLastWin32Error();
                DestroyIcon(pair.Small);
                throw new Win32Exception(error);
            }
            return pair;
        }

        static bool SetProperty(IPropertyStore store, Guid format, uint id, string text)
        {
            var key = new PropertyKey(format, id);
            var value = new PropVariant { type = 31, text = Marshal.StringToCoTaskMemUni(text) };
            try { return store.SetValue(ref key, ref value) >= 0; }
            finally { Marshal.FreeCoTaskMem(value.text); }
        }

        static bool SetTaskbarIdentity(IntPtr hwnd, string iconPath)
        {
            IPropertyStore store = null;
            try {
                Guid iid = typeof(IPropertyStore).GUID;
                if (SHGetPropertyStoreForWindow(hwnd, ref iid, out store) < 0 || store == null) return false;
                Guid format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
                // RelaunchIconResource lets the shell obtain the source ICO
                // without relying on Chromium's rasterized favicon cache.
                bool icon = SetProperty(store, format, 3, iconPath + ",0");
                bool identity = SetProperty(store, format, 5, "Verilo.Desktop");
                return icon && identity && store.Commit() >= 0;
            }
            catch (COMException) { return false; }
            catch (DllNotFoundException) { return false; }
            catch (EntryPointNotFoundException) { return false; }
            finally { if (store != null) Marshal.ReleaseComObject(store); }
        }

        public static int Run(int browserProcessId, string iconPath)
        {
            using (var browser = Process.GetProcessById(browserProcessId)) {
                // Keep a handle to this process instance so PID reuse cannot
                // cause the helper to adopt an unrelated browser later.
                IntPtr processHandle = browser.Handle;
                IntPtr hwnd = IntPtr.Zero;
                var deadline = Stopwatch.StartNew();
                while (!browser.HasExited && deadline.Elapsed < TimeSpan.FromSeconds(30)) {
                    hwnd = FindWindow(browserProcessId);
                    if (hwnd != IntPtr.Zero) break;
                    Thread.Sleep(250);
                }
                if (hwnd == IntPtr.Zero) {
                    Console.WriteLine("VERILO_WINDOW_ICON success=false");
                    return 1;
                }

                IntPtr originalSmall, originalBig;
                bool gotSmall = Message(hwnd, WM_GETICON, 0, IntPtr.Zero, out originalSmall);
                bool gotBig = Message(hwnd, WM_GETICON, 1, IntPtr.Zero, out originalBig);
                var loaded = new Dictionary<uint, IconPair>();
                uint previousDpi = 0;
                bool reported = false;
                try {
                    bool identity = SetTaskbarIdentity(hwnd, iconPath);
                    Console.WriteLine("VERILO_TASKBAR_IDENTITY success=" + (identity ? "true" : "false"));
                    while (!browser.HasExited && IsWindow(hwnd) && BelongsTo(hwnd, browserProcessId)) {
                        uint dpi = WindowDpi(hwnd);
                        IconPair icons;
                        if (!loaded.TryGetValue(dpi, out icons)) {
                            icons = LoadIcons(iconPath, dpi);
                            loaded.Add(dpi, icons);
                        }
                        IntPtr currentSmall, currentBig, ignored;
                        bool small = Message(hwnd, WM_GETICON, 0, IntPtr.Zero, out currentSmall);
                        bool big = Message(hwnd, WM_GETICON, 1, IntPtr.Zero, out currentBig);
                        if (!small || currentSmall != icons.Small)
                            small = Message(hwnd, WM_SETICON, 0, icons.Small, out ignored);
                        if (!big || currentBig != icons.Big)
                            big = Message(hwnd, WM_SETICON, 1, icons.Big, out ignored);
                        if ((!reported || previousDpi != dpi) && small && big) {
                            Console.WriteLine("VERILO_WINDOW_ICON hwnd=0x" + hwnd.ToInt64().ToString("x") +
                                " small=" + icons.SmallWidth + "x" + icons.SmallHeight +
                                " big=" + icons.BigWidth + "x" + icons.BigHeight + " success=true");
                            reported = true;
                            previousDpi = dpi;
                        }
                        Thread.Sleep(1000);
                    }
                    return reported ? 0 : 1;
                }
                finally {
                    // A window may outlive a failing helper. Restore only the
                    // icons read from this window before releasing our handles.
                    bool canRelease = !IsWindow(hwnd) || !BelongsTo(hwnd, browserProcessId);
                    if (!canRelease && gotSmall && gotBig) {
                        IntPtr ignored;
                        bool restoredSmall = Message(hwnd, WM_SETICON, 0, originalSmall, out ignored);
                        bool restoredBig = Message(hwnd, WM_SETICON, 1, originalBig, out ignored);
                        canRelease = restoredSmall && restoredBig;
                    }
                    if (canRelease) foreach (var icons in loaded.Values) {
                        DestroyIcon(icons.Small);
                        DestroyIcon(icons.Big);
                    }
                    // If the target is hung and restoration failed, retain the
                    // handles until helper-process cleanup instead of explicitly
                    // destroying an HICON still referenced by the target.
                }
            }
        }
    }
}
'@ -ErrorAction Stop
    }
    $veriloIconExit = [Verilo.NativeWindowIcon]::Run($BrowserProcessId, $veriloIconFile)
    exit $veriloIconExit
} catch {
    # Avoid printing exception arguments, process command lines or filesystem
    # paths. Native failures are represented only by the sanitized outcome.
    Write-Output 'VERILO_WINDOW_ICON success=false'
    exit 1
}
