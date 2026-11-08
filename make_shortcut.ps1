# Creates "Govee lights" shortcuts on the Desktop and in the Start menu, with the rainbow-bulb icon.
# Right-click either one -> Pin to Start, or (Show more options ->) Pin to taskbar.
# Re-run if Python is reinstalled:  powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (& python -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"  # the Python that has bleak installed
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

# A shortcut needs the same AppUserModelID as the window ("Govee.Lights", set in app.pyw), so the
# pinned icon and the open window share one taskbar button. WScript.Shell cannot set that, so:
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace GoveeShortcut {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class CShellLink {}
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
  interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder f, int c, IntPtr fd, uint fl);
    void GetIDList(out IntPtr p); void SetIDList(IntPtr p);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder s, int c);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder s, int c);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder s, int c);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string s);
    void GetHotkey(out short k); void SetHotkey(short k);
    void GetShowCmd(out int c); void SetShowCmd(int c);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder s, int c, out int i);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string s, int i);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string s, uint r);
    void Resolve(IntPtr hwnd, uint fl);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string s);
  }
  [StructLayout(LayoutKind.Sequential, Pack = 4)] struct PropertyKey { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)] struct PropVariant {
    [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; [FieldOffset(16)] public IntPtr pad;
  }
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
  interface IPropertyStore {
    void GetCount(out uint c); void GetAt(uint i, out PropertyKey k); void GetValue(ref PropertyKey k, out PropVariant v);
    void SetValue(ref PropertyKey k, ref PropVariant v); void Commit();
  }
  public static class Maker {
    public static void Create(string lnk, string target, string args, string dir, string icon, string desc, string appId) {
      var link = (IShellLinkW)new CShellLink();
      link.SetPath(target); link.SetArguments(args); link.SetWorkingDirectory(dir);
      link.SetIconLocation(icon, 0); link.SetDescription(desc);
      var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };  // AppUserModel.ID
      var value = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(appId) };                 // VT_LPWSTR
      try { var store = (IPropertyStore)link; store.SetValue(ref key, ref value); store.Commit(); }
      finally { Marshal.FreeCoTaskMem(value.p); }
      ((IPersistFile)link).Save(lnk, true);
    }
  }
}
'@

# Windows does not keep taskbar pins whose target is a Store-Python alias, so the shortcuts point
# at a tiny real launcher, "Govee lights.exe" (with the icon inside), which starts pythonw app.pyw.
$launcher = @"
using System; using System.Diagnostics; using System.IO;
static class GoveeLauncher {
  [STAThread] static void Main() {
    string dir = AppDomain.CurrentDomain.BaseDirectory;
    string py = @"$pythonw";
    if (!File.Exists(py)) py = "pythonw.exe";
    try {
      Process.Start(new ProcessStartInfo(py, "\"" + Path.Combine(dir, "app.pyw") + "\"") {
        WorkingDirectory = dir, UseShellExecute = false });
    } catch (Exception e) {
      System.Windows.Forms.MessageBox.Show("Could not start Python (" + py + "):\n" + e.Message, "Govee lights");
    }
  }
}
"@
$exe = Join-Path $here "Govee lights.exe"
$cp = New-Object System.CodeDom.Compiler.CompilerParameters
$cp.GenerateExecutable = $true
$cp.OutputAssembly = $exe
$cp.CompilerOptions = "/target:winexe /optimize /win32icon:`"$here\govee.ico`""
[void]$cp.ReferencedAssemblies.Add("System.dll")
[void]$cp.ReferencedAssemblies.Add("System.Windows.Forms.dll")
$res = (New-Object Microsoft.CSharp.CSharpCodeProvider).CompileAssemblyFromSource($cp, $launcher)
if ($res.Errors.HasErrors) { $res.Errors | ForEach-Object { $_.ToString() }; throw "could not build the launcher" }
"built $exe"

foreach ($folder in [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs")) {
    $lnk = Join-Path $folder "Govee lights.lnk"
    [GoveeShortcut.Maker]::Create($lnk, $exe, "", $here, "$here\govee.ico",
        "Control the Govee bulbs over Bluetooth", "Govee.Lights")
    "created $lnk"
}
