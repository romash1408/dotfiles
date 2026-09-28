-- Собирает шаблон проекта «Клавиши» (ProjectTemplates/keys.RPP) в запущенном Reaper:
--   reaper -nonewinst ~/.local/share/chezmoi/.reaper/build-keys-template.lua
-- Состояния плагинов не выдуманы, а собраны из форматов самих плагинов и
-- проверены рендером (28.09.2026):
--   * sfizz (VST3): состояние — бинарь SfizzVstState (sfizz-ui 1.2.3,
--     plugins/vst/SfizzVstState.cpp); берём заводское и вписываем путь к .sfz.
--   * Vital (CLAP): состояние — ровно JSON пресета, тот же формат, что у .vital.
-- Путь к SFZ — /opt/keys-station/samples/...: одинаковый на hp и на маке (там
-- это симлинк), поэтому бинарное состояние sfizz переносимо без шаблонизации.

local SFZ = "/opt/keys-station/samples/SalamanderGrandPiano/SalamanderGrandPiano-V3+20200602.sfz"
local PAD_NAME = "Analog Pad"
local PAD_FILE = "In The Mix/Presets/Analog Pad.vital"

-- Крутилки по стандарту CC (layout.toml в infra, заметка «Раскладка Oxygen 49»):
-- CC → макрос Vital (параметры 211–214 у Vital 1.6.4 CLAP). На Oxygen 49 это
-- C11–C13; под драйв (70) исправной крутилки у него нет, привязка — для других.
local PAD_KNOBS = {
  { cc = 74, param = 211 },  -- яркость  → BRIGHTNESS
  { cc = 91, param = 212 },  -- реверб   → REVERB
  { cc = 94, param = 213 },  -- дилей    → DELAY
  { cc = 70, param = 214 },  -- драйв    → DRIVE
}

local ALL_MIDI_ALL_CH = 4096 + (63 << 5)   -- REC-вход: все MIDI-устройства, все каналы

local function fail(msg)
  reaper.ShowConsoleMsg("build-keys-template: " .. msg .. "\n")
  error(msg)
end

-- base64 (в Lua у Reaper его нет)
local B = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local function b64dec(s)
  s = s:gsub("[^%w%+/=]", "")
  return (s:gsub(".", function(c)
    if c == "=" then return "" end
    local n = B:find(c, 1, true) - 1
    local r = ""
    for i = 6, 1, -1 do r = r .. ((n >> (i - 1)) & 1) end
    return r
  end):gsub("%d%d%d?%d?%d?%d?%d?%d?", function(bits)
    if #bits ~= 8 then return "" end
    return string.char(tonumber(bits, 2))
  end))
end
local function b64enc(data)
  return ((data:gsub(".", function(c)
    local r, b = "", c:byte()
    for i = 8, 1, -1 do r = r .. ((b >> (i - 1)) & 1) end
    return r
  end) .. "0000"):gsub("%d%d%d?%d?%d?%d?", function(bits)
    if #bits < 6 then return "" end
    return B:sub(tonumber(bits, 2) + 1, tonumber(bits, 2) + 1)
  end) .. ({ "", "==", "=" })[#data % 3 + 1])
end

local function read_file(path)
  local f = io.open(path, "rb") or fail("не открыть " .. path)
  local d = f:read("a"); f:close(); return d
end

local function vital_data_dir()
  local home = os.getenv("HOME")
  if reaper.GetOS():match("^OSX") or reaper.GetOS():match("^mac") then
    return home .. "/Music/Vital/"
  end
  return home .. "/.local/share/vital/"
end

local function learn(track, fx, param, cc)
  local p = "param." .. param .. ".learn."
  reaper.TrackFX_SetNamedConfigParm(track, fx, p .. "midi1", "176")        -- 0xB0: CC, канал 1
  reaper.TrackFX_SetNamedConfigParm(track, fx, p .. "midi2", tostring(cc))
  reaper.TrackFX_SetNamedConfigParm(track, fx, p .. "mode", "0")           -- absolute
  reaper.TrackFX_SetNamedConfigParm(track, fx, p .. "flags", "2")          -- soft takeover
end

local function setup_track(track, name, selected)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", name, true)
  reaper.SetMediaTrackInfo_Value(track, "I_RECINPUT", ALL_MIDI_ALL_CH)
  reaper.SetMediaTrackInfo_Value(track, "I_RECMODE", 0)     -- запись входа
  reaper.SetMediaTrackInfo_Value(track, "I_RECMON", 1)      -- мониторинг входа
  reaper.SetMediaTrackInfo_Value(track, "I_RECARM", 1)
  reaper.SetMediaTrackInfo_Value(track, "I_SELECTED", selected and 1 or 0)
end

reaper.Main_OnCommand(40859, 0)  -- File: New project tab
local proj = 0
reaper.InsertTrackAtIndex(0, true)
reaper.InsertTrackAtIndex(1, true)
local piano, pad = reaper.GetTrack(proj, 0), reaper.GetTrack(proj, 1)

-- Рояль: sfizz + Salamander
local fx = reaper.TrackFX_AddByName(piano, "VST3:sfizz (SFZTools)", false, -1)
if fx < 0 then fail("sfizz (VST3) не найден — проверить vstpath") end
local ok, chunk = reaper.TrackFX_GetNamedConfigParm(piano, fx, "vst_chunk")
if not ok then fail("нет vst_chunk у sfizz") end
local raw = b64dec(chunk)
-- рамка Reaper: int32 размер состояния компонента, int32 флаг, состояние, хвост
local size = string.unpack("<i4", raw, 1)
local comp, tail = raw:sub(9, 8 + size), raw:sub(9 + size)
-- состояние: uint64 версия, затем str8 (int32 длина с \0, байты) — путь к .sfz
if comp:sub(9, 13) ~= "\1\0\0\0\0" then fail("неожиданное состояние sfizz: путь уже задан?") end
local path = SFZ .. "\0"
local newcomp = comp:sub(1, 8) .. string.pack("<i4", #path) .. path .. comp:sub(14)
reaper.TrackFX_SetNamedConfigParm(piano, fx, "vst_chunk",
  b64enc(string.pack("<i4i4", #newcomp, 1) .. newcomp .. tail))
setup_track(piano, "Рояль", true)

-- Пэд: Vital + фабричный Analog Pad
fx = reaper.TrackFX_AddByName(pad, "CLAP:Vital (Vital Audio)", false, -1)
if fx < 0 then fail("Vital (CLAP) не найден") end
local preset = read_file(vital_data_dir() .. PAD_FILE)
-- в фабричном файле нет preset_name — впишем, чтобы Vital показывал имя
if not preset:find('"preset_name"') then
  preset = preset:gsub("^%s*{", '{"preset_name":"' .. PAD_NAME .. '",', 1)
end
reaper.TrackFX_SetNamedConfigParm(pad, fx, "clap_chunk", b64enc(preset))
for _, k in ipairs(PAD_KNOBS) do learn(pad, fx, k.param, k.cc) end
setup_track(pad, "Пэд", false)
-- Пэд по умолчанию не слышно: подмешивается фейдером 2 (CC 21)
reaper.SetMediaTrackInfo_Value(pad, "D_VOL", 0)

local dir = reaper.GetResourcePath() .. "/ProjectTemplates"
reaper.RecursiveCreateDirectory(dir, 0)
reaper.Main_SaveProjectEx(proj, dir .. "/keys.RPP", 0)
reaper.ShowConsoleMsg("build-keys-template: сохранено " .. dir .. "/keys.RPP\n")
