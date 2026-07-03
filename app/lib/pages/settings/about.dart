import 'package:omi/utils/platform/platform_manager.dart';
import 'package:flutter/material.dart';

import 'package:url_launcher/url_launcher.dart';

import 'package:omi/pages/settings/webview.dart';
import 'package:omi/services/app_update_service.dart';
import 'package:omi/utils/analytics/intercom.dart';
import 'package:omi/utils/l10n_extensions.dart';
import 'package:omi/utils/other/temp.dart';

class AboutOmiPage extends StatefulWidget {
  const AboutOmiPage({super.key});

  @override
  State<AboutOmiPage> createState() => _AboutOmiPageState();
}

class _AboutOmiPageState extends State<AboutOmiPage> {
  bool _checking = false;

  Future<void> _checkForUpdates() async {
    if (_checking) return;
    setState(() => _checking = true);
    PlatformManager.instance.analytics.pageOpened('About Check For Updates');
    final result = await AppUpdateService.instance.checkForUpdate();
    if (!mounted) return;
    setState(() => _checking = false);

    if (result.updateAvailable && result.info != null) {
      _showUpdateDialog(result.info!);
    } else if (result.error != null) {
      _showInfoDialog(context.l10n.checkForUpdates, context.l10n.somethingWentWrong);
    } else {
      _showInfoDialog(context.l10n.checkForUpdates, context.l10n.appUpToDate);
    }
  }

  void _showInfoDialog(String title, String message) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: Text(context.l10n.ok)),
        ],
      ),
    );
  }

  void _showUpdateDialog(AppUpdateInfo info) {
    double progress = 0;
    bool downloading = false;
    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (ctx) {
        return StatefulBuilder(
          builder: (ctx, setDialogState) {
            return AlertDialog(
              title: Text(context.l10n.updateAvailable),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(context.l10n.updateAvailableDescription(info.versionName)),
                  if (downloading) ...[
                    const SizedBox(height: 16),
                    LinearProgressIndicator(value: progress > 0 ? progress : null),
                    const SizedBox(height: 8),
                    Text('${(progress * 100).clamp(0, 100).toStringAsFixed(0)}%'),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: downloading ? null : () => Navigator.pop(ctx),
                  child: Text(context.l10n.cancel),
                ),
                TextButton(
                  onPressed: downloading
                      ? null
                      : () async {
                          setDialogState(() => downloading = true);
                          try {
                            final file = await AppUpdateService.instance.downloadApk(
                              info,
                              onProgress: (received, total) {
                                if (total > 0) setDialogState(() => progress = received / total);
                              },
                            );
                            final ok = await AppUpdateService.instance.installApk(file);
                            if (ctx.mounted) Navigator.pop(ctx);
                            if (!ok && mounted) {
                              _showInfoDialog(context.l10n.updateAvailable, context.l10n.somethingWentWrong);
                            }
                          } catch (e) {
                            if (ctx.mounted) Navigator.pop(ctx);
                            if (mounted) {
                              _showInfoDialog(context.l10n.updateAvailable, context.l10n.somethingWentWrong);
                            }
                          }
                        },
                  child: Text(context.l10n.update),
                ),
              ],
            );
          },
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Theme.of(context).colorScheme.primary,
      appBar: AppBar(title: Text(context.l10n.aboutOmi), backgroundColor: Theme.of(context).colorScheme.primary),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.privacyPolicy, style: const TextStyle(color: Colors.white)),
              trailing: const Icon(Icons.privacy_tip_outlined, size: 20),
              onTap: () {
                PlatformManager.instance.analytics.pageOpened('About Privacy Policy');
                routeToPage(
                  context,
                  PageWebView(url: 'https://www.omi.me/pages/privacy', title: context.l10n.privacyPolicyTitle),
                );
              },
            ),
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.visitWebsite, style: const TextStyle(color: Colors.white)),
              subtitle: const Text('https://omi.me'),
              trailing: const Icon(Icons.language_outlined, size: 20),
              onTap: () {
                PlatformManager.instance.analytics.pageOpened('About Visit Website');
                // routeToPage(context, const PageWebView(url: 'https://www.omi.me/', title: 'omi'));
                launchUrl(Uri.parse('https://www.omi.me/'));
              },
            ),
            ListTile(
              title: Text(context.l10n.helpOrInquiries, style: const TextStyle(color: Colors.white)),
              subtitle: const Text('team@basedhardware.com'),
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              trailing: const Icon(Icons.help_outline_outlined, color: Colors.white, size: 20),
              onTap: () async {
                await IntercomManager.instance.intercom.displayMessenger();
              },
            ),
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.joinCommunity, style: const TextStyle(color: Colors.white)),
              subtitle: Text(context.l10n.membersAndCounting),
              trailing: const Icon(Icons.discord, color: Colors.purple, size: 20),
              onTap: () {
                PlatformManager.instance.analytics.pageOpened('About Join Discord');
                launchUrl(Uri.parse('http://discord.omi.me'));
              },
            ),
            ListTile(
              contentPadding: const EdgeInsets.fromLTRB(4, 0, 24, 0),
              title: Text(context.l10n.checkForUpdates, style: const TextStyle(color: Colors.white)),
              trailing: _checking
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white),
                    )
                  : const Icon(Icons.system_update_outlined, color: Colors.white, size: 20),
              onTap: _checking ? null : _checkForUpdates,
            ),
          ],
        ),
      ),
    );
  }
}
