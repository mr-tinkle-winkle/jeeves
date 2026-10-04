# The jeeves package: daemon + GUI + overlay (one Python application) with the
# command-line tools it drives put on its PATH.
{ lib
, python3
, makeWrapper
, qt6
, makeDesktopItem
, copyDesktopItems
# runtime tools (each optional feature degrades gracefully if one is missing)
, whisper-cpp
, llama-cpp
, piper-tts
, espeak-ng
, tesseract
, wl-clipboard
, xclip
, libnotify
, pipewire
, pulseaudio
, grim
, kdotool
, kdePackages ? null
, extraRuntimePackages ? [ ]
}:

let
  py = python3.pkgs;
  optionalPy = names: lib.concatMap (n: lib.optional (py ? ${n}) py.${n}) names;
in
py.buildPythonApplication {
  pname = "jeeves";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ../.;

  build-system = [ py.setuptools ];
  dependencies = [ py.pyside6 py.evdev ] ++ optionalPy [ "vosk" "libzim" ];

  nativeBuildInputs = [ makeWrapper copyDesktopItems qt6.wrapQtAppsHook ];
  buildInputs = [ qt6.qtbase qt6.qtwayland ];

  # one wrapper with both the Qt plugin paths and the runtime PATH
  dontWrapQtApps = true;
  preFixup = ''
    makeWrapperArgs+=("''${qtWrapperArgs[@]}")
  '';

  makeWrapperArgs = [
    "--prefix" "PATH" ":" (lib.makeBinPath ([
      whisper-cpp llama-cpp piper-tts espeak-ng tesseract wl-clipboard xclip libnotify
      pipewire pulseaudio grim kdotool
    ] ++ lib.optional (kdePackages != null && kdePackages ? spectacle) kdePackages.spectacle
      ++ extraRuntimePackages))
  ];

  desktopItems = [
    (makeDesktopItem {
      name = "jeeves";
      exec = "jeeves";
      icon = "audio-input-microphone";
      desktopName = "Jeeves";
      comment = "Voice assistant settings";
      categories = [ "Utility" ];
      startupWMClass = "jeeves";
    })
  ];

  nativeCheckInputs = [ py.pytest ];
  checkPhase = ''
    runHook preCheck
    export HOME=$TMPDIR QT_QPA_PLATFORM=offscreen
    pytest -q tests -k "not gui"
    runHook postCheck
  '';

  meta = {
    description = "Local-first voice assistant for Linux: agents, wake words, composable functions";
    mainProgram = "jeeves";
    platforms = lib.platforms.linux;
  };
}
