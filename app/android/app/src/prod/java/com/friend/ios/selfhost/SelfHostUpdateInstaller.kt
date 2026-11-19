package com.friend.ios.selfhost

import android.app.Activity
import io.flutter.embedding.engine.FlutterEngine

/** Store builds never sideload updates; the self-host installer exists only in the dev flavor. */
object SelfHostUpdateInstaller {
    @Suppress("UNUSED_PARAMETER")
    fun register(flutterEngine: FlutterEngine, activity: Activity) = Unit
}
