#!/bin/bash
umask 077
chmod 700 /run/initramfs || exit 2

pw_cache=/run/initramfs/.password.cache
pw_seed=/run/initramfs/.password.seed

rsa_info=$(grep -o 'rd.rsa=[^[:space:]]\+' /proc/cmdline)
rsa_info=${rsa_info#*=}

rsa_src=${rsa_info%/*}
rsa_dev=${rsa_src%-*}
rsa_offset=${rsa_src#*-}
rsa_key=${rsa_info#*/}

key_file=


process_seed() {
	# Create old/new seed/keys
	pkcs15-crypt -r0 -k "$rsa_key" --sign -i "$pw_seed" -R |
			hexdump -e '"%x"' >"${pw_cache}.old"
	[[ $? -ne 0 || ${PIPESTATUS[0]} -ne 0 || ! -s "${pw_cache}.old" ]] && return 1
	dd if=/dev/urandom of="$pw_seed" bs=256 count=1 status=noxfer 2>/dev/null \
		&& pkcs15-crypt -r0 -k "$rsa_key" --sign -i "$pw_seed" -R |
			hexdump -e '"%x"' >"${pw_cache}.new"
	[[ $? -ne 0 || ${PIPESTATUS[0]} -ne 0 || ! -s "${pw_cache}.new" ]] && return 1
	return 0
}

update_seed() {
	slots=( "$@" )

	# Devices to process
	devs=( $(awk 'match($1, /luks-(.*)/, a) {system("blkid -U " a[1])}' /etc/crypttab) )
	[[ ${#devs[@]} -eq 0 ]] && return 0

	# Key material
	key=( -d "${pw_cache}.old" )
	[[ -n "$key_file" ]] && {
		[[ ! -e "$key_file" ]] && { echo >&2 "Keyfile not found: $key_file"; return 1; }
		key=( -d "$key_file" )
	}

	# Kill old slots
	[[ ${#slots[@]} -gt 0 ]] && {
		for dev in ${devs[@]}; do for slot in ${slots[@]}; do
			cryptsetup -q "${key[@]}" luksKillSlot "$dev" "$slot"
		done; done
	}

	# Add new key, counting failures
	failures=0
	for dev in ${devs[@]}; do
		cryptsetup -q -i100 "${key[@]}" luksAddKey "$dev" "${pw_cache}.new"
		[[ "$?" -ne 0 ]] && (( failures += 1 ))
	done
	[[ $failures -gt 0 ]] && echo >&2 "*** Failed to add new key to $failures devices ***"
	[[ $failures -eq ${#devs[@]} ]] && return 1

	# Remove old keys
	[[ ${#slots[@]} -eq 0 ]] && {
		failures=0
		for dev in ${devs[@]}; do
			cryptsetup -q luksRemoveKey "$dev" "${pw_cache}.old"
			[[ "$?" -ne 0 ]] && (( failures += 1 ))
		done
		[[ $failures -gt 0 ]] && echo >&2 "*** Failed to remove old key from $failures devices ***"
		[[ $failures -eq ${#devs[@]} ]] && return 1
	}

	# Update original seed
	dd if="$pw_seed" of=/dev/"$rsa_dev"\
			bs=256 seek="$rsa_offset" count=1 status=noxfer 2>/dev/null \
		|| return 1

	return 0
}


### Automated start with no arguments

[[ -z "$1" ]] && {
	# Do the thing only if dracut has created a seed file
	err=
	[[ -f "$pw_seed" && -n "$rsa_info" ]] && {
		[[ -z "$err" ]] && process_seed\
			|| { echo >&2 "Failed to process rsa seed"; err=true; }
		[[ -z "$err" ]] && update_seed\
			|| { echo >&2 "Failed to update rsa seed"; err=true; }
	}

	rm -f "$pw_seed" "$pw_cache"{,.old,.new}

	[[ -z "$err" ]] && exit 0 || exit 1
}


### Manual start

while [[ -n "$1" ]]; do
	case "$1" in
		-h|--help) echo "Usage: $0 $(awk '
				func print_arg() {
					if (!ac) ac=""; else ac=sprintf(" ...(%s)", ac)
					if (ap) printf("[ %s%s ] ", ap, ac) }
				/^\s+case\>/ {parse=1; next}
				/^\s+esac\>/ {print_arg(); exit}
				!parse {next}
				match($0, /^\s*([\-|a-z]+)\)/, a) { print_arg(); ap=a[1]; ac=0 }
				!match(ap,/\<-h|--help\>/)\
					{for (i=1;i<NF;i++) if (match($i, /\<shift\>/)) ac++}'\
			$0)"
			exit 0 ;;
		-d|--debug) set -x ;;
		--key-file) shift; key_file="$1" ;;
		rewrite)
			shift
			slots=( "$@" )
			dd if=/dev/urandom of="$pw_seed" bs=256 count=1 status=noxfer 2>/dev/null
			process_seed || { echo >&2 "Failed to process rsa seed"; exit 1; }
			update_seed "${slots[@]}" || { echo >&2 "Failed to rewrite luks slots"; exit 1; }
			rm -f "$pw_seed" "$pw_cache"{,.old,.new}
			exit 0 ;;
		*) echo >&2 "Unknown arg/option: $1" && exit 1 ;;
	esac
	shift
done
