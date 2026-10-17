## About

This is an addon for [qToggleServer](https://github.com/qtoggle/qtoggleserver).

It provides ports that are backed by configurable HTTP requests.


## Install

Install using pip:

    pip install qtoggleserver-generic-http


## Usage

##### `qtoggleserver.conf:`
``` ini
...
peripherals = [
    ...
    {
        driver = "qtoggleserver.generichttp.GenericHTTPClient"
        name = "myperipheral"  # a name of your choice
        read = {                                        # may be omitted, see below
            url = "https://api.example.com/myresource"
            method = GET  # default
            query = {
                name1 = "value1"
                ...
            }
            headers = {
                name2 = "value2"
                ...
            }
            cookies = {
                name3 = "value3"
                ...
            }
            request_body = {  # JSON or custom body string content
                "name4": "value4"
            }
        }
        write = {
            url = "https://api.example.com/myresource"  # inherited from read, if unspecified
            method = POST  # default
            query = {
                name1 = "value1"
                ...
            }
            headers = {
                name2 = "value2"
                ...
            }
            cookies = {
                name3 = "value3"
                ...
            }
            request_body = {  # JSON or custom body string content
                "name4": "value4"
            }
        }
        auth = {
            type = basic  # none (default) or basic
            username = "your_username"
            password = "your_password"
        }
        ignore_response_code = true  # see below, defaults to false
        ignore_invalid_cert = true   # whether to ignore TLS cerfificate issues or not, defaults to false
        timeout = 10                 # default request timeout is 10 seconds
        ports = {
            "port_id1" = {
                type = boolean                # boolean or number
                writable = true               # defaults to true if a port write section is given, false otherwise
                read = {                      # may be omitted, see below
                    url = "https://api.example.com/other"  # own read request for this port, see below
                    json_path = "/path/to/field"  # RFC6901 JSON pointer to port value, inside response body
                    body_regex = "myvalue=(\d+)"  # regular expression inside body for port value lookup
                    true_value = 1                # value or list of values that are true (for boolean ports)
                    false_value = 1               # value or list of values that are false (for boolean ports)
                }
                write = {
                    ...  # overrides to common write details above
                }
            }
            "port_id2" = {
                type = number                 # the limits below only make sense for number ports
                min = 0                       # optional lower limit
                max = 100                     # optional upper limit
                integer = true                # whether values are integers, defaults to false
                step = 5                      # optional value step
                read = {
                    json_path = "/path/to/another/field"
                }
            }
            ...
        }
    }
    ...
]
...
```

### Reading without a read section

Some HTTP integrations can't give a value back. Leave out the port `read` section for those. The port then reports the
last value written to it. Before the first write, its value is unavailable. The last written value is remembered across
restarts.

Such ports can sit next to regular ones in the same peripheral. The peripheral keeps polling its read URL, the regular
ports take their values from the response, and the read-less ones report what was last written to them.

You can leave out the peripheral `read` section as well. Then nothing is polled, except the URLs the ports bring
themselves. In that case:

 * every port read section needs an `url` of its own
 * `write.url` can no longer be inherited from `read.url`, so every writable port needs a write URL

A port with no read section that isn't writable can neither be read nor written, so it is rejected.

### Reading from several URLs

By default every port takes its value from the peripheral's read response. A port that needs another URL can put a
`url` in its own read section. That port then gets a read request of its own, made on every poll.

The peripheral's read section supplies the defaults for `method`, `query`, `headers`, `cookies` and `request_body`, and
the port's own values win. Ports asking for the very same request share it, so one request is made per poll no matter
how many ports read from it. Ports that differ in any of those details get a request each, even when the URL is the
same.

A port that only needs another `json_path` or `body_regex` adds no request at all. One that also sets its own `query`
does. Set an inherited value to `null` to drop it, for example `request_body = null` for a port that reads with `GET`.

`write.url` is still inherited from the peripheral's `read.url`, never from the port's own. So give a write section its
own `url` whenever the port reads from an URL of its own.

The peripheral's own read request is made on every poll even when no port reads from it. When every port is
write-only, it is the only request, so it is what takes the peripheral offline once the device stops answering.

When a read request fails, only the ports reading from it are affected. They keep their last value for as many failures
in a row as `retry_count` allows (none by default), and then lose it. The peripheral goes offline only when every read
request fails.

After a port is written, only the read request of that port is made again. Ports reading from other requests catch up
on the next poll.

Two things to keep in mind:

 * a read request may be shared by several ports, so only the placeholders that don't belong to a port work in one
 * all read requests share the same poll interval, so use separate peripherals if you need different rates

A port read section without a `url` still needs the peripheral to have one.

### Placeholders

Placeholders are based on the [Jinja2](https://jinja.palletsprojects.com/) template rendering engine.

Any of the following fields may be given as templates containing _placeholders_:
 * `url`
 * `query`
 * `headers`
 * `cookies`
 * `request_body`

The following context variables are recognized when replacing placeholders:
 * `value` - the current port value
 * `new_value` - the new port value, available only when writing a value to port
 * `port` - the port itself
 * `attrs` - a dictionary with current port's attributes
 * `device_attrs` - a dictionary with device attributes (slave devices are referenced using `<slave_name>:<attr_name>`
   prefix)
 * `port_attrs` - a dictionary indexed by port id, containing current attributes of each port
 * `port_values` - a dictionary indexed by port id, containing current value of each port
 * `metadata` - a dictionary with all the metadata catalog entries
 
Complex data structures containing lists or dictionaries will be parsed recursively and placeholders will be replaced
in each element or key.

For example, following request body will send the new value in a dictionary field called `"value"`:

    request_body = {
        "value": "{{new_value}}"
    }

All builtin Python functions are available to be used inside the placeholder expression. For more details, see the
[Jinja2 Template Designer Docs](https://jinja.palletsprojects.com/en/2.11.x/templates/).

Template strings containing placeholders must be enclosed in quotes. However, their final value will not be surrounded
by quotes unless it's a string itself. If you really want quotes around your placeholder's real value, you can simply
ensure that the final value is a string, by passing it to the builtin Python function `str` (e.g. `{{str(new_value)}}`).

Another important note is that numeric port values are most often represented internally by a floating-point datatype,
even when the actual value has no decimals (e.g. `3.0`). So `{{new_value}}` would result in `3.0` instead of `3` in this
case. If you need a pure integer value, just cast it to integer: `{{int(new_value)}}`.

### Port Value Readings

Port values are read using the response to an HTTP request. One request serves all the ports that want the very same
one, see [Reading from several URLs](#reading-from-several-urls). Intermediate
_raw values_ are determined from the response and are coerced to the data types of the respective ports.

A raw value is determined as follows:
 * if `ignore_response_code` is `false` (default) and status code is >= 400, the raw value is set to `false`, regardless
of the response body
 * otherwise, if `json_path` is specified, response body is interpreted as JSON and the raw value is looked up using
given JSON path (RFC6901 JSON pointer); if the lookup does not go well (for any reason), the raw value is _undefined_
 * otherwise, if `body_regex` is specified, a regex match is attempted on the entire body content; the first group (or
the entire match, if no group is given) is used to determine the raw value; on unsuccessful match, the raw value is
_undefined_
 * otherwise, the raw value is set to `true` if status code is < 300 and to `false` otherwise (3xx status codes will be
used internally by the HTTP client)

Now given a raw value, the actual port value is determined as follows:
 * if the raw value is _undefined_, the port value becomes _undefined_
 * for a `boolean` port, the value is `true` if the raw value is equal to `true_value` (or one of its items if
`true_value` is a list) and `false` otherwise
 * for a `number` port, the raw value is transformed to a number, unless it already is a number, as follows:
     * `true` is `1` and `false` is `0`
     * strings are converted to numbers, after being stripped of leading and trailing whitespace
     * any other raw value type will result in an _undefined_ port value

### Port Value Writings

Writing port values is done using an HTTP request whose response is ignored (but awaited, up to the given `timeout`).

As opposed to port readings, there will be one separate request for each port whose value needs to be written.

### A Few Remarks

If the request body is not given as a string, it is assumed to be JSON and transmitted as such, including the
corresponding `Content-Type` header set to `application/json`.

