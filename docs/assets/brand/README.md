# CocoaPDF brand assets

Generated brand kit for CocoaPDF: a high-accuracy PDF to Markdown/HTML converter for structured text-layer PDFs. No OCR. No AI.

## Asset concept

The mark combines a folded document page, a red PDF strip, a cocoa bean, and Markdown/region lines. It is designed to stay legible at favicon size while still communicating CocoaPDF's core: deterministic conversion of structured PDF content into editable text formats.

## Repository placement

This bundle is already in the repository at:

```text
CocoaPDF/
  docs/
    assets/
      brand/
        logo/
        icons/
        images/
        badges/
        source/
```

The root `README.md` should reference the README hero as:

```md
<p align="center">
  <img src="docs/assets/brand/images/readme/cocoapdf-readme-hero-1600x640.png" alt="CocoaPDF" width="100%" />
</p>
```

Canonical project file references:

```text
README.md                                      -> docs/assets/brand/images/readme/cocoapdf-readme-hero-1600x640.png
GitHub social preview                         -> docs/assets/brand/images/social/cocoapdf-github-social-preview-1280x640.png
Docs / website header logo                    -> docs/assets/brand/logo/cocoapdf-logo-horizontal.svg
Docs / website favicon directory              -> docs/assets/brand/icons/favicon/
Desktop/GUI app icon source                   -> docs/assets/brand/icons/app/cocoapdf-app-icon.svg
Windows app icon                              -> docs/assets/brand/icons/app/cocoapdf-app-icon.ico
PyPI long_description README image            -> docs/assets/brand/images/readme/cocoapdf-readme-banner-1920x640.png
CLI docs / release pages                      -> docs/assets/brand/images/ui/cocoapdf-cli-header-1024x256.png
Badges                                        -> docs/assets/brand/badges/*.svg
Brand source/tokens                           -> docs/assets/brand/source/cocoapdf-brand-tokens.json
```

This README is the canonical placement guide for brand assets. Do not maintain a second duplicate guide under `docs/assets/brand/docs/`; it drifts and creates architecture noise.

## Important naming rule

Do not rename favicon files unless your docs/static-site framework requires it. Browser and PWA tooling expects names like:

```text
favicon.ico
favicon-16x16.png
favicon-32x32.png
apple-touch-icon.png
android-chrome-192x192.png
android-chrome-512x512.png
site.webmanifest
```

For app icons and README assets, the filenames are intentionally explicit and stable:

```text
cocoapdf-app-icon-256x256.png
cocoapdf-logo-horizontal.svg
cocoapdf-readme-hero-1600x640.png
cocoapdf-og-image-1200x630.png
```

## License

These generated assets are provided for the CocoaPDF project under the same MIT license as the project unless you choose a different project-specific brand policy. No external stock artwork or font files are included.
