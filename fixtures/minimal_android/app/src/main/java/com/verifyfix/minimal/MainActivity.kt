package com.verifyfix.minimal

import android.app.Activity
import android.os.Bundle

class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        openSettings()
    }

    private fun openSettings() {
        showSettings()
    }

    private fun showSettings() {
        setContentView(R.layout.activity_main)
    }
}
