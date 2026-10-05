# NixOS module: services.jeeves
#
#   imports = [ inputs.jeeves.nixosModules.default ];
#   services.jeeves = {
#     enable = true;
#     user = "yourname";
#     settings = {                      # every key set here is LOCKED in the GUI
#       models.stt.model = "whisper-base-en";
#       agents.jeeves.call_names = [ "Jeeves" "Butler" ];
#       run_command.trusted = [ "puppetry --list" "obs" ];
#     };
#     geminiApiKeyFile = "/run/secrets/gemini";   # optional
#   };
{ config, lib, pkgs, ... }:

let
  cfg = config.services.jeeves;
  json = pkgs.formats.json { };
in
{
  options.services.jeeves = {
    enable = lib.mkEnableOption "the Jeeves voice assistant (daemon, overlay and settings GUI)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { inherit (cfg) acceleration; };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The jeeves package.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      example = "max";
      description = ''
        The user Jeeves runs for. They are added to the "input" group so the
        daemon can watch the keyboard for keybinds (read-only, never grabbed)
        and create the jeeves-keyboard / jeeves-mouse / jeeves-controller
        virtual devices for Control Mode.
      '';
    };

    acceleration = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "vulkan" "cuda" "rocm" ]);
      default = null;
      example = "vulkan";
      description = ''
        GPU backend for the local models (llama.cpp and whisper.cpp). null = CPU only
        (prebuilt in the binary cache). "vulkan" works on NVIDIA, AMD and Intel GPUs;
        "cuda" (NVIDIA, needs nixpkgs.config.allowUnfree) and "rocm" (AMD) can be faster but
        may have to build from source; the Vulkan builds come from the binary cache.
        Then set Models > GPU layers (99 = as much as fits).
      '';
    };

    inputAccess = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Grant keyboard watching (keybinds) and uinput (Control Mode).";
    };

    settings = lib.mkOption {
      type = json.type;
      default = { };
      example = lib.literalExpression ''
        {
          wake_word.global_threshold = 0.7;
          models.intent.model = "qwen2.5-1.5b";
          agents.claude = { name = "Claude"; call_names = [ "Claude" ]; };
        }
      '';
      description = ''
        Declarative Jeeves settings, using the same keys as
        ~/.config/jeeves/settings.json (see jeeves/config.py). They are
        written to /etc/jeeves/settings.json; every key declared here is shown
        locked in the GUI and can't be changed from the app.
      '';
    };

    geminiApiKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "File containing a Google AI Studio API key (kept out of the Nix store if you use a secrets manager path).";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    environment.etc."jeeves/settings.json".source = json.generate "jeeves-settings.json" cfg.settings;

    boot.kernelModules = lib.mkIf cfg.inputAccess [ "uinput" ];
    services.udev.extraRules = lib.mkIf cfg.inputAccess ''
      KERNEL=="uinput", MODE="0660", GROUP="input", TAG+="uaccess"
      SUBSYSTEM=="input", ATTRS{name}=="jeeves-mouse*", ENV{ID_INPUT_JOYSTICK}="", ENV{ID_INPUT_MOUSE}="1"
    '';
    users.users.${cfg.user}.extraGroups = lib.mkIf cfg.inputAccess [ "input" ];

    systemd.user.services.jeeves = {
      description = "Jeeves voice assistant daemon";
      wantedBy = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      after = [ "graphical-session.target" "pipewire.service" ];
      # NixOS gives user services a bare PATH: add the system and user profiles so the daemon
      # finds nvidia-smi, kscreen-doctor and the apps Run Command / Open App start
      path = [ "/run/wrappers" "/etc/profiles/per-user/${cfg.user}" "/run/current-system/sw" ];
      environment = {
        JEEVES_SYSTEM_SETTINGS = "/etc/jeeves/settings.json";
      } // lib.optionalAttrs (cfg.geminiApiKeyFile != null) {
        JEEVES_GEMINI_API_KEY_FILE = toString cfg.geminiApiKeyFile;
      };
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/jeeves daemon";
        Restart = "on-failure";
        RestartSec = 2;
      };
    };
  };
}
