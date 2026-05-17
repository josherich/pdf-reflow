# AppIcon

Drop a single **1024 × 1024 PNG** here named `AppIcon.png`.

Xcode synthesises every smaller size (notification, settings, spotlight,
home-screen marketing) from this one master image at build time — the
single-size flow is the supported path for Xcode 14+, so no other files
are needed.

If you want light/dark/tinted variants for iOS 18+, replace
`Contents.json` with one that has three entries:

```json
{
  "images" : [
    { "filename": "AppIcon.png",        "idiom": "universal", "platform": "ios", "size": "1024x1024" },
    { "filename": "AppIcon-Dark.png",   "idiom": "universal", "platform": "ios", "size": "1024x1024",
      "appearances": [ { "appearance": "luminosity", "value": "dark" } ] },
    { "filename": "AppIcon-Tinted.png", "idiom": "universal", "platform": "ios", "size": "1024x1024",
      "appearances": [ { "appearance": "luminosity", "value": "tinted" } ] }
  ],
  "info" : { "author" : "xcode", "version" : 1 }
}
```

The current design (dark green with a glassy PDF document marked by
reflow lines) already works well in both light and dark home screens,
so a single entry is fine.
