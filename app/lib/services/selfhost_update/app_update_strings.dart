/// English-only copy for the self-host updater.
///
/// Kept out of the upstream ARB catalog on purpose: the feature exists only in
/// the self-host dev build, and adding keys there would regenerate every locale
/// file on each upstream sync.
abstract final class SelfHostUpdateStrings {
  static const String checkForUpdates = 'Check for updates';
  static const String checking = 'Checking…';
  static const String updateAvailable = 'Update available';
  static const String upToDate = 'You are on the latest build.';
  static const String notPublished = 'No update has been published yet.';
  static const String checkFailed = 'Could not check for updates. Try again later.';
  static const String later = 'Later';
  static const String update = 'Update';
  static const String install = 'Install';
  static const String ok = 'OK';
  static const String downloading = 'Downloading…';
  static const String verifying = 'Verifying…';
  static const String permissionRequired =
      'Allow Omi Dev to install apps in the screen that just opened, then come back and tap Install.';

  static String availableDescription(String versionName) =>
      'Omi Dev $versionName is ready. It downloads over your self-hosted server, is checked against its '
      'published SHA-256, and installs over this build without clearing your data.';

  static String notificationBody(String versionName) => 'Omi Dev $versionName is ready to install.';

  static String currentBuild(String versionName, int versionCode) => 'Installed: $versionName ($versionCode)';

  static String failure(String reason) {
    switch (reason) {
      case 'checksum_mismatch':
        return 'The download did not match its published checksum and was discarded.';
      case 'size_mismatch':
      case 'size_exceeded':
        return 'The download size did not match the published build and was discarded.';
      case 'signer_mismatch':
        return 'This build is signed with a different key and cannot replace the installed app.';
      case 'package_mismatch':
        return 'The published build is for a different app package.';
      case 'not_newer':
        return 'The published build is not newer than the installed app.';
      default:
        return 'The update could not be completed ($reason). Try again later.';
    }
  }
}
