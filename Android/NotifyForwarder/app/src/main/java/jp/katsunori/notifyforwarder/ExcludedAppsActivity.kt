package jp.katsunori.notifyforwarder

import android.content.pm.PackageManager
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * インストール済みアプリの一覧から、SMS転送の対象外にしたいアプリを
 * チェックボックスで選べる設定画面。
 */
class ExcludedAppsActivity : AppCompatActivity() {

    private data class AppEntry(val label: String, val packageName: String)

    private lateinit var allApps: List<AppEntry>
    private var filteredApps: MutableList<AppEntry> = mutableListOf()
    private lateinit var adapter: AppListAdapter
    private val excludedPackages = mutableSetOf<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_excluded_apps)

        val prefs = getSharedPreferences(
            NotificationForwarderService.PREFS_NAME, MODE_PRIVATE
        )
        excludedPackages.addAll(
            prefs.getStringSet(NotificationForwarderService.KEY_EXCLUDED_PACKAGES, emptySet())
                ?: emptySet()
        )

        allApps = loadInstalledApps()
        filteredApps = allApps.toMutableList()

        adapter = AppListAdapter()
        findViewById<ListView>(R.id.listApps).adapter = adapter

        findViewById<EditText>(R.id.editSearch).addTextChangedListener(object : TextWatcher {
            override fun afterTextChanged(s: Editable?) {
                val query = s?.toString()?.trim()?.lowercase() ?: ""
                filteredApps = if (query.isEmpty()) {
                    allApps.toMutableList()
                } else {
                    allApps.filter {
                        it.label.lowercase().contains(query) ||
                            it.packageName.lowercase().contains(query)
                    }.toMutableList()
                }
                adapter.notifyDataSetChanged()
            }

            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
        })

        findViewById<View>(R.id.buttonSaveExcluded).setOnClickListener {
            prefs.edit()
                .putStringSet(NotificationForwarderService.KEY_EXCLUDED_PACKAGES, excludedPackages)
                .apply()
            Toast.makeText(this, "保存しました", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    /** 自分自身を除いた、インストール済みアプリの一覧をアプリ名順に取得する */
    private fun loadInstalledApps(): List<AppEntry> {
        val pm: PackageManager = packageManager
        return pm.getInstalledApplications(PackageManager.GET_META_DATA)
            .filter { it.packageName != packageName }
            .map { AppEntry(pm.getApplicationLabel(it).toString(), it.packageName) }
            .sortedBy { it.label.lowercase() }
    }

    private inner class AppListAdapter : ArrayAdapter<AppEntry>(
        this@ExcludedAppsActivity,
        R.layout.item_excluded_app
    ) {
        override fun getCount(): Int = filteredApps.size
        override fun getItem(position: Int): AppEntry = filteredApps[position]

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: LayoutInflater.from(context)
                .inflate(R.layout.item_excluded_app, parent, false)

            val entry = filteredApps[position]
            val checkBox = view.findViewById<CheckBox>(R.id.checkboxApp)
            val labelView = view.findViewById<TextView>(R.id.textAppLabel)
            val packageView = view.findViewById<TextView>(R.id.textAppPackage)

            labelView.text = entry.label
            packageView.text = entry.packageName

            // リストの使い回し(リサイクル)で誤って前の状態が反映されないよう、
            // 一旦リスナーを外してから状態をセットする
            checkBox.setOnCheckedChangeListener(null)
            checkBox.isChecked = excludedPackages.contains(entry.packageName)
            checkBox.setOnCheckedChangeListener { _, isChecked ->
                if (isChecked) {
                    excludedPackages.add(entry.packageName)
                } else {
                    excludedPackages.remove(entry.packageName)
                }
            }

            return view
        }
    }
}
