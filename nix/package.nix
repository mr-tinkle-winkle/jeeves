# The jeeves package: daemon + GUI + overlay (one Python application) with the
# command-line tools it drives put on its PATH.
{ lib
, python3
, makeWrapper
, stdenv
, cmake
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
, callPackage
, extraRuntimePackages ? [ ]
}:

let
  py = python3.pkgs;
  extras = callPackage ./python-extras.nix { inherit python3; };

  # The overlay's layer-shell shim (native/, same as afterglow's clip indicator).
  # LayerShellQt has a C++ API only, so a tiny C shim is loaded with ctypes. It must use
  # the same Qt as PySide6 -- both come from this nixpkgs.
  layerShell = if kdePackages != null && kdePackages ? layer-shell-qt then stdenv.mkDerivation {
    pname = "jeeves-layershell";
    version = "0.1.0";
    src = ../native;
    nativeBuildInputs = [ cmake ];
    buildInputs = [ qt6.qtbase kdePackages.layer-shell-qt ];
    dontWrapQtApps = true;
  } else null;
in
py.buildPythonApplication {
  pname = "jeeves";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ../.;

  build-system = [ py.setuptools ];
  dependencies = [ py.pyside6 py.evdev ] ++ lib.filter (p: p != null) [ extras.vosk extras.libzim ];

  nativeBuildInputs = [ makeWrapper copyDesktopItems ];

  makeWrapperArgs = [
    # native Wayland for the settings window and the indicator overlay
    # (+ layer-shell-qt's wayland-shell-integration plugin for the indicator overlay)
    "--prefix" "QT_PLUGIN_PATH" ":" (lib.makeSearchPath qt6.qtbase.qtPluginPrefix
      ([ qt6.qtbase qt6.qtwayland ] ++ lib.optional (layerShell != null) kdePackages.layer-shell-qt))
    # the daemon starts the overlay processes through this wrapper
    "--set-default" "JEEVES_BIN" "${placeholder "out"}/bin/jeeves"
  ] ++ lib.optionals (layerShell != null) [
    "--set-default" "JEEVES_LAYERSHELL_LIB" "${layerShell}/lib/libjeeves_layershell.so"
  ] ++ [
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
