# A small, predictable Windows app for testing the desktop agent (Phase 11.2).
# Plain Windows Forms controls, so UI Automation sees them like any normal app:
# text boxes, a drop-down, a check box, a password box, buttons, a long list, a modal dialog.
# Usage: powershell -ExecutionPolicy Bypass -File testapp.ps1 [-Title "Jarvis Test App"]
param([string]$Title = "Jarvis Test App")

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 720; $form.Height = 560
$form.StartPosition = "CenterScreen"
$form.TopMost = $false

function Add-Label($text, $x, $y) {
  $l = New-Object System.Windows.Forms.Label
  $l.Text = $text; $l.Left = $x; $l.Top = $y; $l.AutoSize = $true
  $form.Controls.Add($l); return $l
}

Add-Label "Your name" 20 22 | Out-Null
$name = New-Object System.Windows.Forms.TextBox
$name.Left = 120; $name.Top = 20; $name.Width = 220; $name.AccessibleName = "Your name"
$form.Controls.Add($name)

Add-Label "Country" 20 62 | Out-Null
$country = New-Object System.Windows.Forms.ComboBox
$country.Left = 120; $country.Top = 58; $country.Width = 220
$country.DropDownStyle = "DropDownList"; $country.AccessibleName = "Country"
[void]$country.Items.AddRange(@("India", "Nepal", "Sri Lanka"))
$country.SelectedIndex = 0
$form.Controls.Add($country)

$news = New-Object System.Windows.Forms.CheckBox
$news.Text = "Send me news"; $news.Left = 120; $news.Top = 96; $news.Width = 200
$form.Controls.Add($news)

Add-Label "Password" 20 136 | Out-Null
$pw = New-Object System.Windows.Forms.TextBox
$pw.Left = 120; $pw.Top = 132; $pw.Width = 220; $pw.UseSystemPasswordChar = $true
$pw.AccessibleName = "Password"
$form.Controls.Add($pw)

$status = Add-Label "Ready" 20 480  # its text is what UI Automation reads (no fixed name)

$save = New-Object System.Windows.Forms.Button
$save.Text = "Save"; $save.Left = 120; $save.Top = 172; $save.Width = 100
$save.Add_Click({ $status.Text = "Saved: " + $name.Text + ", " + $country.SelectedItem })
$form.Controls.Add($save)

$del = New-Object System.Windows.Forms.Button
$del.Text = "Delete account"; $del.Left = 240; $del.Top = 172; $del.Width = 120
$del.Add_Click({ $status.Text = "Account deleted" })
$form.Controls.Add($del)

$prefs = New-Object System.Windows.Forms.Button
$prefs.Text = "Preferences"; $prefs.Left = 380; $prefs.Top = 172; $prefs.Width = 110
$prefs.Add_Click({
  # a modal dialog: the main window can't be used until it is closed (like Save As, Options…)
  $dlg = New-Object System.Windows.Forms.Form
  $dlg.Text = "Preferences"; $dlg.Width = 320; $dlg.Height = 180; $dlg.StartPosition = "CenterParent"
  $dark = New-Object System.Windows.Forms.CheckBox
  $dark.Text = "Dark mode"; $dark.Left = 20; $dark.Top = 20; $dark.Width = 200
  $dlg.Controls.Add($dark)
  $ok = New-Object System.Windows.Forms.Button
  $ok.Text = "OK"; $ok.Left = 20; $ok.Top = 70
  $ok.Add_Click({ $dlg.Close() })
  $dlg.Controls.Add($ok)
  [void]$dlg.ShowDialog($form)
  $status.Text = "Dark mode: " + $(if ($dark.Checked) { "on" } else { "off" })
})
$form.Controls.Add($prefs)

$attach = New-Object System.Windows.Forms.Button
$attach.Text = "Attach file..."; $attach.Left = 510; $attach.Top = 172; $attach.Width = 110
$attach.Add_Click({
  # the standard Windows Open dialog, like any app's Attach / Browse
  $open = New-Object System.Windows.Forms.OpenFileDialog
  $open.Title = "Open"
  if ($open.ShowDialog($form) -eq "OK") { $status.Text = "Attached: " + $open.FileName }
})
$form.Controls.Add($attach)

Add-Label "Songs" 20 220 | Out-Null
$songs = New-Object System.Windows.Forms.ListBox
$songs.Left = 120; $songs.Top = 220; $songs.Width = 320; $songs.Height = 230
$songs.AccessibleName = "Songs"
$titles = @("Shape of You", "Perfect", "Believer", "Faded", "Closer", "Despacito", "Senorita",
  "Levitating", "Peaches", "Stay", "Heat Waves", "Bad Habits", "Easy On Me", "As It Was",
  "Anti-Hero", "Flowers", "Calm Down", "Kesariya", "Tum Hi Ho", "Apna Bana Le", "Chaleya",
  "Jhoome Jo Pathaan", "Kill Bill", "Cruel Summer", "Vampire", "Paint The Town Red",
  "Espresso", "Blinding Lights", "Die With A Smile", "APT.")
[void]$songs.Items.AddRange($titles)
$form.Controls.Add($songs)

$play = New-Object System.Windows.Forms.Button
$play.Text = "Play"; $play.Left = 460; $play.Top = 220; $play.Width = 100
$play.Add_Click({
  if ($songs.SelectedItem) { $status.Text = "Playing " + $songs.SelectedItem }
  else { $status.Text = "Pick a song first" }
})
$form.Controls.Add($play)
$songs.Add_DoubleClick({ if ($songs.SelectedItem) { $status.Text = "Playing " + $songs.SelectedItem } })

[void]$form.ShowDialog()
