# Third-party notices and source provenance

This archive preserves reviewed source and local adaptations. It contains no blanket license grant for all components. Each component retains its original license, copyright and notice obligations. Original license and NOTICE files distributed alongside a component are authoritative; this summary does not replace them.

## Git-based upstreams

- AstrBot: https://github.com/AstrBotDevs/AstrBot.git — AGPL-3.0-or-later. The [dependency lock](upstream/DEPENDENCIES.lock.json) records the reviewed upstream commit; reviewed local source overlays under `upstream/overlays/AstrBot/` remain subject to its applicable terms.
- dsh-bili-humanism: https://github.com/InariNGT-Ofiicial/dsh-bili-humanism.git — MIT. Retain its LICENSE and NOTICE; reviewed local source overlays are under `upstream/overlays/dsh-bili-humanism/`.
- Netease API Enhanced: https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced.git — MIT. The upstream is an external dependency; the local service adapter is a separate component whose root license has not been selected here.

## Plugin components

| Component | License evidence in the reviewed installation |
| --- | --- |
| `astrbot_plugin_ai_cover` | External dependency; source not redistributed because no applicable license grant was found |
| `astrbot_plugin_bilibili_ai_bot` | MIT |
| `astrbot_plugin_image_companion` | External dependency; source not redistributed because no applicable license grant was found |
| `astrbot_plugin_private_companion` | External dependency; source not redistributed because no applicable license grant was found |
| `astrbot_plugin_proactive_chat` | AGPL v3 license text |
| `astrbot_plugin_qzone` | GPL v3 license text |
| `astrbot_plugin_remember_you` | External dependency; source not redistributed because no applicable license grant was found |
| `astrbot_plugin_self_learning` | AGPL v3 license text; material-icons Apache-2.0 notice |
| `astrbot_plugin_tieba_lens` | MIT; OFL/font and vendor notices |
| `astrbot_plugin_virtual_life` | GPL v3 license text |
| `astrbot_plugin_xiaoheihe_adapter` | MIT with THIRD_PARTY_NOTICES |
| `feiyu_agent_soul` | No local root license grant found |
| `feiyu_steward` | No local root license grant found; artwork separately licensed and excluded |
| `meme_manager` | MIT |

The public archive excludes all original and locally modified source of four third-party components: `astrbot_plugin_ai_cover`, `astrbot_plugin_image_companion`, `astrbot_plugin_private_companion`, and `astrbot_plugin_remember_you`. GitHub REST reported accessible repositories with no declared license and no license file at the license endpoint. Their public source links are recorded in the dependency lock for provenance only. Obtain applicable permission independently before using or redistributing their source; this archive grants no rights to it. These excluded components are required for some features described by the surrounding integration source, so those features are not provided as complete implementations here.

The GPL/AGPL entries above identify the version of the supplied license text. Preserve the upstream's actual per-file and project grant; do not infer an `-or-later` grant solely from a copy of the license text. Network-service and modified-source distribution obligations must be assessed under the applicable license. MIT components require retention of the original notice. Vendored source, fonts and optional visual assets may have separate notices; keep applicable notices when including those resources.

## Non-Git source snapshots and external dependencies

- dsh-bili-scroll, version metadata 0.1.18: https://github.com/c1overzz/dsh-bili-scroll — MIT, with separate LICENSE/NOTICE for bundled bilibili-mcp.
- dsh-agent-soul, version metadata 0.1.0: https://github.com/cyanwell/dsh-agent-soul — MIT, with separate bundled component licenses including huanqi.
- dsh-video-storyboard, version metadata 0.5.12: https://github.com/SAOS-3624/dsh-video-storyboard — Apache-2.0; retain LICENSE, NOTICE and applicable attributions.
- RVC cover service: the legacy `tools/uvr5/` subtree is excluded because the external source/permission for its `bsroformer.py` attribution was not established. The current cover chain uses the separately licensed pymss implementation. The installation points to https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI and supplies an MIT license. The exact origin and base of the service wrapper are not established. Retain independent licenses for bundled pymss and other inference components. Voice models, recordings, training data and binaries are excluded.
- GPT-SoVITS: https://github.com/RVC-Boss/GPT-SoVITS — external dependency with MIT at the top level and independent licenses for bundled subcomponents. Its exact installed source base is not established here; models, voice data and binaries are excluded.
- SnowLuma: external dependency only. The reviewed distribution identifies version 2.0.3 and references a LICENSE file that was absent. Its supplied EULA restricts public distribution of modified builds and proprietary modules. This archive does not redistribute that runtime, bundle or proprietary modules and grants no rights to them.
- QQ and stable-diffusion-cpp runtime distributions are external installations. Client executables, native DLLs and model weights are excluded; users must obtain compatible distributions under their own terms.
- NapCatQQ: https://github.com/NapNeko/NapCatQQ — the deployed bundle is excluded. A directory label suggests 4.18.41 but does not establish its exact source base. Obtain the applicable upstream source/license and a compatible version before applying an independently reviewed integration delta.

For non-Git snapshots, package version strings are provenance hints, not proof of an exact upstream commit or of unmodified source. Their reviewed current source is preserved under `upstream/snapshots/` after redaction so local changes are not silently lost. No clean reconstruction or deployment has been verified.

## Bundled chart code

The legacy vendor `echarts.min.js` declares ECharts 5.4.3 and ZRender 5.4.4. Exact-tag license/notice files are retained under `plugins/astrbot_plugin_self_learning/web_res/static/vendor/licenses/echarts-5.4.3/` and `zrender-5.4.4/`. ECharts uses Apache-2.0 and ZRender uses BSD-3-Clause. Preserve their source headers and these original notices. The old tree-shaken dashboard chart chunk has no independently established version and is excluded; rebuild the frontend from the retained source and lockfile.

## Original local glue

The adapters under `upstream/local/`, native source under `integrations/native-bridge/`, `feiyu_agent_soul`, `feiyu_steward` and maintenance/launch scripts contain original local adaptations published at the project owner's explicit direction. No blanket license is selected by this notice, and it does not replace any upstream component's license.

Native launch integrity checks pin hashes of both custom modules and the upstream bundle. Redaction or reconstruction changes those hashes. Regenerate the manifest only from the complete reviewed rebuilt file set and retain the integrity checks. This archive does not include account configuration, runtime receipts, credentials or production ledgers.
