# The release version: the date of the commit the flake was fetched at, as
# YY.MM.DD. A tree without a date (a bare path) is "dev", like a plain go build.
{ self }:
let
  stamp = self.lastModifiedDate or null;
  part = from: builtins.substring from 2 stamp;
in
if stamp == null then "dev" else "${part 2}.${part 4}.${part 6}"
