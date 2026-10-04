# Python packages nixpkgs doesn't have, built from their official PyPI wheels
# (both ship a prebuilt native library; autoPatchelfHook points it at Nix's libs).
#   vosk   -- the wake word spotter
#   libzim -- reading the offline Wikipedia (ZIM)
# Returns null for a package when there's no wheel for this platform/Python,
# and Jeeves runs without that feature.
{ lib, stdenv, python3, fetchurl, autoPatchelfHook }:

let
  py = python3.pkgs;
  arch = stdenv.hostPlatform.parsed.cpu.name;   # x86_64 | aarch64
  pyTag = "cp" + lib.replaceStrings [ "." ] [ "" ] python3.pythonVersion;

  voskWheels = {
    x86_64 = {
      url = "https://files.pythonhosted.org/packages/fc/ca/83398cfcd557360a3d7b2d732aee1c5f6999f68618d1645f38d53e14c9ff/vosk-0.3.45-py3-none-manylinux_2_12_x86_64.manylinux2010_x86_64.whl";
      sha256 = "25e025093c4399d7278f543568ed8cc5460ac3a4bf48c23673ace1e25d26619f";
    };
    aarch64 = {
      url = "https://files.pythonhosted.org/packages/a4/23/3130a69fa0bf4f5566a52e415c18cd854bf561547bb6505666a6eb1bb625/vosk-0.3.45-py3-none-manylinux2014_aarch64.whl";
      sha256 = "54efb47dd890e544e9e20f0316413acec7f8680d04ec095c6140ab4e70262704";
    };
  };

  libzimWheels = {
    "cp313-x86_64" = {
      url = "https://files.pythonhosted.org/packages/4a/45/2327aae1d51dca8324f0ba7a91abd7bf24a2e43b93cf2e1f851e0962eee7/libzim-3.13.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl";
      sha256 = "8cb2938aa95e105617c347a344ea51058cc6799b68b01dafc0ba9474092afff1";
    };
    "cp313-aarch64" = {
      url = "https://files.pythonhosted.org/packages/e1/da/a724452de45fdbd7d610515123c0c3596cb2c44dc9c6ef617f1355b469f0/libzim-3.13.0-cp313-cp313-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl";
      sha256 = "3379aeebfc7fc21b13f9b7e2e1cb802ef39edf28732387fe2249cc208d51e0b3";
    };
    "cp314-x86_64" = {
      url = "https://files.pythonhosted.org/packages/16/51/c59a8fc8a2ce3d43cfe699185b53a37da73547b63341383d147ba9d1a887/libzim-3.13.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl";
      sha256 = "86ac9f35c9944c52d46095cd10980d8852d6a6aefe1fc1aa9bafde034dea84f8";
    };
    "cp314-aarch64" = {
      url = "https://files.pythonhosted.org/packages/95/f7/003e91bcfbcb0722520f13ca912f61aa0066cc66b1bd0d249fef23d0e7e5/libzim-3.13.0-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl";
      sha256 = "c5a5dc2eddd1615520bd67d2b35d8acc08d430ccd401dad51c9ecb2af338b055";
    };
  };

  wheel = { pname, version, src, dependencies ? [ ] }: py.buildPythonPackage {
    inherit pname version src dependencies;
    format = "wheel";
    nativeBuildInputs = [ autoPatchelfHook ];
    buildInputs = [ stdenv.cc.cc.lib ];
    pythonImportsCheck = [ pname ];
  };
in
{
  vosk =
    if voskWheels ? ${arch} then
      wheel {
        pname = "vosk";
        version = "0.3.45";
        src = fetchurl voskWheels.${arch};
        dependencies = [ py.cffi py.requests py.tqdm py.srt py.websockets ];
      }
    else null;

  libzim =
    if libzimWheels ? "${pyTag}-${arch}" then
      wheel {
        pname = "libzim";
        version = "3.13.0";
        src = fetchurl libzimWheels."${pyTag}-${arch}";
      }
    else null;
}
