$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$code = @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
public static class VeriloIconBuilder {
  public static void Build(string path) {
    int[] sizes = { 16, 20, 24, 32, 40, 48, 64, 96, 128, 256 };
    byte[][] images = new byte[sizes.Length][];
    for (int i = 0; i < sizes.Length; i++) {
      int n = sizes[i];
      using (var bitmap = new Bitmap(n, n, PixelFormat.Format32bppArgb))
      using (var g = Graphics.FromImage(bitmap))
      using (var white = new SolidBrush(Color.FromArgb(255,255,255)))
      using (var green = new SolidBrush(Color.FromArgb(36,91,53)))
      using (var tile = new GraphicsPath())
      using (var shape = new GraphicsPath())
      using (var ms = new MemoryStream()) {
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.ScaleTransform(n / 64f, n / 64f);
        g.Clear(Color.Transparent);
        tile.AddArc(0,0,22,22,180,90); tile.AddArc(42,0,22,22,270,90);
        tile.AddArc(42,42,22,22,0,90); tile.AddArc(0,42,22,22,90,90); tile.CloseFigure();
        g.FillPath(green,tile);
        shape.StartFigure();
        shape.AddLine(7,37,17,33);
        shape.AddBezier(17,33,20,23,23,20,26,20);
        shape.AddBezier(26,20,30,19,32,23,34,26);
        shape.AddBezier(34,26,39,22,43,25,46,28);
        shape.AddLine(46,28,56,31);
        shape.AddBezier(56,31,59,32,59,35,57,38);
        shape.AddLine(57,38,32,38);
        shape.AddBezier(32,38,39,42,47,44,55,43);
        shape.AddLine(55,43,53,47);
        shape.AddBezier(53,47,40,51,25,47,15,43);
        shape.AddLine(15,43,7,41); shape.CloseFigure();
        g.FillPath(white,shape);
        g.FillEllipse(green,31,28,4.5f,4.5f);
        bitmap.Save(ms, ImageFormat.Png); images[i] = ms.ToArray();
      }
    }
    using (var f = File.Create(path))
    using (var w = new BinaryWriter(f)) {
      w.Write((ushort)0); w.Write((ushort)1); w.Write((ushort)sizes.Length);
      int offset = 6 + 16 * sizes.Length;
      for (int i = 0; i < sizes.Length; i++) {
        w.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
        w.Write((byte)(sizes[i] == 256 ? 0 : sizes[i]));
        w.Write((byte)0); w.Write((byte)0); w.Write((ushort)1); w.Write((ushort)32);
        w.Write(images[i].Length); w.Write(offset); offset += images[i].Length;
      }
      foreach (var bytes in images) w.Write(bytes);
    }
  }
}
'@
Add-Type -TypeDefinition $code -ReferencedAssemblies 'System.Drawing'
[VeriloIconBuilder]::Build((Join-Path (Split-Path $PSScriptRoot) 'src/loom/web/verilo.ico'))
Copy-Item -LiteralPath (Join-Path (Split-Path $PSScriptRoot) 'src/loom/web/verilo.ico') -Destination (Join-Path (Split-Path $PSScriptRoot) 'src/loom/web/verilo-taskbar-v4.ico') -Force
